"""Feed simulated full-screen apps (dashboard, log viewer, chat, editor, boxed input) into the
daemon's terminal replay and check the rebuilt history: right length, one footer, no repeats.

Usage: python3 tests/term_replay_sim.py orbitd.py
"""
import os, re, sys, collections, random
src = open(sys.argv[1]).read()
start = src.index('try:\n    import pyte'); end = src.index('TERM_RECORDINGS = {}')
ns = {}; exec('import codecs, os, re, threading\n' + src[start:end], ns)
ROWS, COLS = 30, 100

def run(frames):
    r = ns['TermRecording']('/nonexistent'); r.size = [COLS, ROWS]
    r.main = r._new_screen(True)
    r._feed_text('\x1b[?1049h')
    for f in frames:
        r._feed_text('\x1b[H' + '\r\n'.join(line.ljust(COLS)[:COLS] for line in f))
        r._snapshot_alt()
    lines, total, _ = r.render(10000)
    return [re.sub(r'\x1b\[[0-9;:]*m', '', l).rstrip() for l in lines]

def report(name, out, expect, footer_markers):
    feet = {m: sum(1 for l in out if m in l) for m in footer_markers}
    w = collections.Counter(tuple(out[i:i+3]) for i in range(len(out)-2) if all(x.strip() for x in out[i:i+3]))
    dups = sum(c - 1 for c in w.values() if c > 1)
    print(f'{name:<22} history {len(out):>4} lines (expect ~{expect}) | footer copies {feet} | repeated 3-line blocks {dups}')

random.seed(1)
# A: htop-like dashboard, nothing scrolls, numbers change
A = [[f'  CPU[{"|" * random.randint(1, 30):<30}] {random.randint(0,100)}%', '  Mem[|||||||||        3.1G/23G]', ''] +
     [f'{1000+i:>6} user  {random.random()*10:5.1f}  {random.random()*5:4.1f}  proc-{i}' for i in range(ROWS - 4)] +
     ['F1Help  F2Setup  F3Search  F9Kill  F10Quit'] for _ in range(60)]
report('A dashboard (htop)', run(A), ROWS, ['F10Quit'])

# B: log viewer scrolling 3 lines per frame, status line at bottom
log = [f'[{i:05d}] event happened with value {i*7 % 101}' for i in range(400)]
B = [log[k:k + ROWS - 1] + [f':  line {k}  (press q to quit)'] for k in range(0, 300, 3)]
report('B scrolling log', run(B), 330, ['press q to quit'])

# C: chat app, messages scroll up, footer = blank + "> input" + status, no rule lines
msgs = []
C = []
for k in range(80):
    msgs += [f'user: question number {k}', f'bot: answer {k} part one', f'     answer {k} part two', '']
    view = msgs[-(ROWS - 3):]
    C.append(view + [''] * (ROWS - 3 - len(view)) + ['', f'> typing {"." * (k % 4)}', f'model x · {k} msgs · ctrl+c to quit'])
report('C chat (no rules)', run(C), 320, ['ctrl+c to quit'])

# D: vim paging down a 300-line file, status bar + mode line
src_lines = [f'def function_{i}(x):  # line {i}' for i in range(300)]
D = [src_lines[k:k + ROWS - 2] + [f'file.py  [+]   {k},1   {k*100//300}%', '-- INSERT --' if k % 2 else ''] for k in range(0, 270, 9)]
report('D editor (vim)', run(D), 300, ['file.py  [+]'])

# E: chat app with a boxed input (rounded corners) and a status line
msgs = []; E = []
for k in range(80):
    msgs += [f'> question {k}', f'  reply {k} line one', f'  reply {k} line two', '']
    view = msgs[-(ROWS - 4):]
    E.append(view + [''] * (ROWS - 4 - len(view)) + ['╭' + '─' * 60 + '╮', f'│ > draft {k % 3}'.ljust(61) + '│', '╰' + '─' * 60 + '╯', f'agent v1 · {k} turns'])
report('E chat (boxed input)', run(E), 320, ['╭'])


# F/G: liveness. The bottom of the phone view must always be the app's current screen,
# even when it switches to another view (e.g. an agent panel) or between snapshots.
def live_check(name, frames, snap_every):
    r = ns['TermRecording']('/nonexistent'); r.size = [COLS, ROWS]
    r.main = r._new_screen(True); r._feed_text('\x1b[?1049h')
    bad = 0
    for k, f in enumerate(frames):
        r._feed_text('\x1b[H\x1b[2J' + '\r\n'.join(line.ljust(COLS)[:COLS] for line in f))
        r.revision += 1
        if k % snap_every == 0:
            r._snapshot_alt()
        out = [re.sub(r'\x1b\[[0-9;:]*m', '', l).rstrip() for l in r.render(10000)[0]]
        want = [x.rstrip() for x in f]
        while want and not want[-1]:
            want.pop()
        want = [x for x, _ in ns['_collapse_blank']([(x, x) for x in want])]   # blank runs show as one
        if out[-len(want):] != want:
            bad += 1
    print(f'{name:<22} frames whose bottom != current screen: {bad} of {len(frames)}')

msgs = []; F = []
for k in range(40):
    msgs += [f'user: q{k}', f'bot: a{k}', '']
    F.append(msgs[-(ROWS - 3):] + ['', '> ', 'status'])
F += [[f'AGENT PANEL row {i}' for i in range(ROWS - 3)] + ['', '> ', 'status']] * 3   # switch view
F += [msgs[-(ROWS - 3):] + ['', '> ', 'status']]                                    # and back
live_check('F view switch', F, 1)
live_check('G between snapshots', C, 3)


# H: scrolled back inside a fullscreen app ("Jump to bottom" showing): an old part of the
# conversation is on screen. History must not change, and must continue in order afterwards.
def scrolled_back_check():
    r = ns['TermRecording']('/nonexistent'); r.size = [COLS, ROWS]
    r.main = r._new_screen(True); r._feed_text('\x1b[?1049h')
    strip = lambda l: re.sub(r'\x1b\[[0-9;:]*m', '', l).rstrip()
    def show(lines):
        r._feed_text('\x1b[H\x1b[2J' + '\r\n'.join(l.ljust(COLS)[:COLS] for l in lines))
        r.revision += 1
        r._snapshot_alt()
    footer = ['─' * 40, '❯', '─' * 40, '  status']
    msgs = [f'message {i}' for i in range(60)]
    for k in range(10, 51, 3):
        show(msgs[k - 10:k] + footer)
    before = [strip(l) for l in r.render(10000)[0]]
    show(msgs[5:14] + ['  Jump to bottom (ctrl+End) ↓'] + footer)
    held = [strip(l) for l in r.render(10000)[0]] == before
    show(msgs[44:56] + footer)
    nums = [int(l.split()[1]) for l in (strip(x) for x in r.render(10000)[0]) if l.startswith('message')]
    ok = held and nums == sorted(nums) and len(nums) == len(set(nums)) and nums[-1] == 55
    print(f'{"H scrolled back":<22} history held while scrolled back, then in order: {ok}')

scrolled_back_check()


# I: an app redrawing the normal screen in place (Claude Code after /resume): jumps to the top
# and rewrites everything, so nothing scrolls into history. It must be stitched like the
# alternate screen, with what the shell printed before it kept above.
def main_redraw_check():
    r = ns['TermRecording']('/nonexistent'); r.size = [COLS, ROWS]
    r.main = r._new_screen(True)
    strip = lambda l: re.sub(r'\x1b\[[0-9;:]*m', '', l).rstrip()
    r._feed_text(''.join(f'shell line {i}\r\n' for i in range(40)) + '$ claude\r\n')
    footer = ['─' * 40, '❯', '─' * 40, '  status']
    msgs = [f'message {i}' for i in range(80)]
    for k in range(20, 71, 3):
        frame = msgs[k - 20:k] + [''] * (ROWS - 24) + footer
        r._feed_text('\x1b[H' + '\r\n'.join(l.ljust(COLS)[:COLS] for l in frame))
        r.revision += 1
        r._snapshot_alt()
    out = [strip(l) for l in r.render(10000)[0]]
    nums = [int(l.split()[1]) for l in out if l.startswith('message')]
    ok = (r.main_tui and nums == sorted(nums) and len(nums) == len(set(nums)) and nums[0] <= 10 and nums[-1] == 67
          and any(l == 'shell line 0' for l in out) and sum(l == '❯' for l in out) == 1)
    print(f'{"I redraw in place":<22} stitched like fullscreen, shell output kept above: {ok}')

main_redraw_check()


# J: scrolling back inside the app with no hint on screen: the screen only shows lines the
# history already has, older than the latest. Nothing new was printed, so the phone's view
# must not change at all; new lines afterwards continue in order.
def scroll_without_hint_check():
    r = ns['TermRecording']('/nonexistent'); r.size = [COLS, ROWS]
    r.main = r._new_screen(True); r._feed_text('\x1b[?1049h')
    strip = lambda l: re.sub(r'\x1b\[[0-9;:]*m', '', l).rstrip()
    def show(lines):
        r._feed_text('\x1b[H\x1b[2J' + '\r\n'.join(l.ljust(COLS)[:COLS] for l in lines))
        r.revision += 1
        r._snapshot_alt()
    footer = ['─' * 40, '❯', '─' * 40, '  status']
    msgs = [f'message {i}' for i in range(80)]
    for k in range(20, 61, 2):
        show(msgs[k - 20:k] + footer)
    before = [strip(l) for l in r.render(10000)[0]]
    held = True
    for top in (35, 30, 22, 10):                 # wheel up, a few steps
        show(msgs[top:top + 20] + footer)
        held = held and [strip(l) for l in r.render(10000)[0]] == before
    show(msgs[44:64] + footer)                   # new output arrives
    nums = [int(l.split()[1]) for l in (strip(x) for x in r.render(10000)[0]) if l.startswith('message')]
    ok = held and nums == sorted(nums) and len(nums) == len(set(nums)) and nums[-1] == 63
    print(f'{"J scroll, no hint":<22} phone unchanged while scrolling, then in order: {ok}')

scroll_without_hint_check()


# K: recordings from orbit-term mark scroll inputs. A big jump to the top right after a scroll
# input is scrolling (the phone holds), a later redraw while still scrolled back holds too, and
# a panel opened with no scroll input is followed.
def input_marks_check():
    import os, tempfile
    d = tempfile.mkdtemp()
    base = os.path.join(d, 'rec')
    log, timing = open(base + '.log', 'wb'), open(base + '.timing', 'w')
    timing.write(f'H 0.000000 COLUMNS {COLS}\nH 0.000000 LINES {ROWS}\nH 0.000000 ORBIT_INPUT_MARKS 1\n')
    def out(text, delay=0.5):
        b = text.encode()
        log.write(b); timing.write(f'O {delay:.6f} {len(b)}\n')
    def screen(lines):
        out('\x1b[H\x1b[2J' + '\r\n'.join(l.ljust(COLS)[:COLS] for l in lines))
    footer = ['─' * 40, '❯', '─' * 40, '  status']
    msgs = [f'message {i}' for i in range(90)]
    out('\x1b[?1049h', 0.1)
    for k in range(20, 61, 2):
        screen(msgs[k - 20:k] + footer)
    log.flush(); timing.flush()
    r = ns['TermRecording'](base); r.update()
    strip = lambda l: re.sub(r'\x1b\[[0-9;:]*m', '', l).rstrip()
    before = [strip(l) for l in r.render(10000)[0]]
    timing.write('X 0.300000 SCROLL top\n')                       # Ctrl+Home: straight to the top
    screen(msgs[0:20] + footer)
    screen(msgs[0:20] + footer[:3] + ['  status ·'])               # spinner tick while up there
    log.flush(); timing.flush(); r.update()
    held = [strip(l) for l in r.render(10000)[0]] == before
    timing.write('X 0.300000 SCROLL bottom\n')                    # Ctrl+End, then new output
    screen(msgs[40:60] + footer)
    screen(msgs[44:64] + footer)
    log.flush(); timing.flush(); r.update()
    nums = [int(l.split()[1]) for l in (strip(x) for x in r.render(10000)[0]) if l.startswith('message')]
    in_order = nums == sorted(nums) and len(nums) == len(set(nums)) and nums[-1] == 63
    screen([f'PANEL row {i}' for i in range(20)] + footer)           # a panel, no scroll input
    log.flush(); timing.flush(); r.update()
    followed = strip(r.render(10000)[0][-5]) == 'PANEL row 19'
    ok = held and in_order and followed
    print(f'{"K scroll inputs":<22} held after scroll input, in order after, panel followed: {ok}')

input_marks_check()
