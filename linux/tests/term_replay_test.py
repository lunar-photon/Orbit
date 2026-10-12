"""Feed a recorded terminal into the daemon's replay and check what the phone gets: shell
output with its scrollback, and a full-screen app's screen exactly as it is (like tmux).

Usage: python3 tests/term_replay_test.py orbitd.py
"""
import os, re, sys, tempfile
src = open(sys.argv[1]).read()
start = src.index('try:\n    import pyte'); end = src.index('TERM_RECORDINGS = {}')
ns = {}; exec('import codecs, os, re, threading\n' + src[start:end], ns)
ROWS, COLS = 10, 40


def record(chunks):
    d = tempfile.mkdtemp()
    base = os.path.join(d, 'rec')
    with open(base + '.log', 'w') as log, open(base + '.timing', 'w') as timing:
        timing.write(f'H 0.000000 COLUMNS {COLS}\nH 0.000000 LINES {ROWS}\n')
        for c in chunks:
            log.write(c)
            timing.write(f'O 0.010000 {len(c.encode())}\n')
    rec = ns['TermRecording'](base)
    rec.update()
    lines, total, _ = rec.render(1000)
    return rec, [re.sub(r'\x1b\[[0-9;:]*m', '', l).rstrip() for l in lines]


def check(name, ok):
    print(f'{name:40} {"ok" if ok else "FAILED"}')
    return ok


shell = ''.join(f'line {i}\r\n' for i in range(30)) + '$ '
app = ['\x1b[?1049h'] + [f'\x1b[H\x1b[2Jframe {n}\r\n' + 'body\r\n' * 5 + '\x1b[10;1H> input' for n in range(20)]
results = []
rec, lines = record([shell])
results.append(check('shell: scrollback kept, in order', lines[:3] == ['line 0', 'line 1', 'line 2'] and lines[-1] == '$'))
rec, lines = record([shell] + app)
results.append(check('app: only its current screen', lines[0] == 'frame 19' and lines.count('frame 18') == 0))
results.append(check('app: bottom row is the input box', lines[-1] == '> input' and len(lines) == ROWS))
results.append(check('app: shown as fullscreen', rec.alt is not None))
rec, lines = record([shell] + app + ['\x1b[?1049l', 'done\r\n$ '])
results.append(check('app quit: shell scrollback back', lines[0] == 'line 0' and lines[-2:] == ['$ done', '$']))
sys.exit(0 if all(results) else 1)
