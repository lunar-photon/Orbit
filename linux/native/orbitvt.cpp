// orbitvt: the terminal emulator behind Orbit's terminal view, as a small shared library.
//
// The service replays each recorded terminal (orbit-term / `script` output) through a screen
// like this to rebuild what the tab shows, with colours and scrollback. It used to be pyte, in
// Python; this does the same job in C++, and renders rows straight to the (plain, ANSI) text the
// phone gets. It follows pyte where pyte is right, so the service's stitching logic sees the same
// screens, and a real terminal (xterm, Konsole) where pyte loses or garbles text:
//   - text after a zero-width character (ZWJ, variation selector) or a stray control is kept
//   - colon-separated SGR (38:2::r:g:b, 4:3), REP, SU/SD, CHT/CBT are understood
//   - DCS/APC/PM/SOS strings (tmux passthrough, kitty graphics) are skipped, not drawn as text
//   - CSI with intermediates (ESC[2 q, ESC[!p) is ignored instead of leaking its final byte
//   - one saved cursor (DECSC/DECRC), as on real terminals, not a stack
//   - shrinking the screen moves top lines into history and keeps the cursor on its line
//   - overwriting half of a wide character blanks the other half
// Python talks to it through ctypes (see orbitd.py: _VtScreen); the C API is at the bottom.

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <deque>
#include <string>
#include <unordered_map>
#include <vector>

#include "wcwidth_table.h"

namespace {

int cp_width(uint32_t cp) {
    if (cp >= 0x20 && cp < 0x7f) return 1;
    size_t lo = 0, hi = sizeof(WIDTHS) / sizeof(WIDTHS[0]);
    while (lo < hi) {
        size_t mid = (lo + hi) / 2;
        if (cp < WIDTHS[mid].first) hi = mid;
        else if (cp > WIDTHS[mid].last) lo = mid + 1;
        else return WIDTHS[mid].width;
    }
    return 1;
}

// A cell: code point (or index of an interned multi-code-point string) plus attributes.
// Colours: 0 default, 1..16 palette entry + 1, RGB_FLAG | 0xRRGGBB true colour.
constexpr uint32_t CP_MASK = 0x1FFFFF, INTERNED = 1u << 21, BOLD = 1u << 22, ITALIC = 1u << 23,
                   UNDER = 1u << 24, REVERSE = 1u << 25, DIM = 1u << 26,
                   ATTR_MASK = BOLD | ITALIC | UNDER | REVERSE | DIM;
constexpr uint32_t RGB_FLAG = 1u << 24;
constexpr uint32_t STUB = 0;       // second cell of a wide character

struct Cell {
    uint32_t ch, fg, bg;
    bool operator==(const Cell &o) const { return ch == o.ch && fg == o.fg && bg == o.bg; }
};
constexpr Cell BLANK{' ', 0, 0};

struct Pen {
    uint32_t flags = 0, fg = 0, bg = 0;
    Cell cell(uint32_t cp) const { return Cell{cp | flags, fg, bg}; }
};

// Cells past the end of `cells` are blank; like pyte's sparse rows, only what was written is
// stored, so the renderer knows where a row's content ends.
struct Line {
    std::vector<Cell> cells;
    bool cont = false;     // this row only exists because the line above hit the right edge
};

void put_utf8(std::string &out, uint32_t cp) {
    if (cp < 0x80) out += char(cp);
    else if (cp < 0x800) { out += char(0xC0 | cp >> 6); out += char(0x80 | (cp & 0x3F)); }
    else if (cp < 0x10000) {
        out += char(0xE0 | cp >> 12); out += char(0x80 | (cp >> 6 & 0x3F)); out += char(0x80 | (cp & 0x3F));
    } else {
        out += char(0xF0 | cp >> 18); out += char(0x80 | (cp >> 12 & 0x3F));
        out += char(0x80 | (cp >> 6 & 0x3F)); out += char(0x80 | (cp & 0x3F));
    }
}

uint32_t palette256(int n) {
    if (n < 16) return uint32_t(n) + 1;        // the terminal's own palette, like 30-37/90-97
    if (n < 232) {
        static const int steps[6] = {0x00, 0x5f, 0x87, 0xaf, 0xd7, 0xff};
        n -= 16;
        return RGB_FLAG | steps[n / 36] << 16 | steps[n / 6 % 6] << 8 | steps[n % 6];
    }
    int v = 8 + (n - 232) * 10;
    return RGB_FLAG | v << 16 | v << 8 | v;
}

enum State { GROUND, ESCAPE, ESC_INTER, CSI_PARAM, OSC_STRING, OSC_ESC, SKIP_STRING, SKIP_ESC };

struct Screen {
    int cols, rows;
    std::vector<Line> buf;
    bool has_history;
    size_t history_max;
    std::deque<Line> history;
    uint64_t history_serial = 0;

    int cx = 0, cy = 0;
    Pen pen;
    bool autowrap = true, insert_mode = false, newline_mode = false, origin = false;
    int mtop = -1, mbot = -1;            // scroll margins; -1: the whole screen
    std::vector<uint8_t> tabs;
    struct Saved { int x = 0, y = 0; Pen pen; bool origin = false, wrap = true, valid = false; } saved;
    std::vector<uint8_t> dirty;
    bool drawing = false;                // inside draw(): a line feed is a wrap
    uint32_t last_cp = 0;

    std::vector<std::string> interned;
    std::unordered_map<std::string, uint32_t> intern_ids;

    // parser
    State state = GROUND;
    uint32_t utf8_cp = 0;
    int utf8_need = 0;
    std::vector<int> params;
    std::vector<uint8_t> sub;            // params[i] followed a ':' (a sub-parameter)
    bool have_param = false, priv = false, priv_other = false, inter = false;
    char esc_inter = 0;

    std::string out;                     // last result handed to Python

    Screen(int c, int r, int hist) : cols(std::max(c, 1)), rows(std::max(r, 1)), buf(rows),
                                     has_history(hist > 0), history_max(hist > 0 ? hist : 0),
                                     dirty(rows, 1) {
        reset_tabs(0);
    }

    // ---- helpers ----
    int top() const { return mtop < 0 ? 0 : mtop; }
    int bottom() const { return mtop < 0 ? rows - 1 : mbot; }
    void mark(int y) { if (y >= 0 && y < rows) dirty[y] = 1; }
    void mark_from(int y) { for (int i = std::max(y, 0); i < rows; i++) dirty[i] = 1; }
    void mark_all() { std::fill(dirty.begin(), dirty.end(), 1); }
    void reset_tabs(int from) {
        tabs.resize(cols);
        for (int x = from; x < cols; x++) tabs[x] = x > 0 && x % 8 == 0;
    }
    Cell &at(Line &l, int x) {
        if (int(l.cells.size()) <= x) l.cells.resize(x + 1, BLANK);
        return l.cells[x];
    }
    void push_history(Line &&l) {
        if (!has_history) return;
        while (!l.cells.empty() && l.cells.back() == BLANK) l.cells.pop_back();
        l.cells.shrink_to_fit();
        history.push_back(std::move(l));
        if (history.size() > history_max) history.pop_front();
        history_serial++;
    }
    void clear_history() {
        if (!history.empty()) history_serial++;
        history.clear();
    }
    uint32_t intern(const std::string &s) {
        auto it = intern_ids.find(s);
        if (it != intern_ids.end()) return it->second;
        if (interned.size() >= CP_MASK) return 0xFFFD;
        uint32_t id = interned.size() | INTERNED;
        interned.push_back(s);
        intern_ids.emplace(s, id);
        return id;
    }
    void cell_text(std::string &s, uint32_t ch) const {
        uint32_t v = ch & CP_MASK;
        if (ch & INTERNED) s += interned[v];
        else put_utf8(s, v);
    }

    // ---- cursor ----
    void clamp_x() { cx = std::min(std::max(cx, 0), cols - 1); }
    void clamp_y() {
        int t = 0, b = rows - 1;
        if (origin && mtop >= 0) { t = mtop; b = mbot; }
        cy = std::min(std::max(cy, t), b);
    }
    void cursor_up(int n) {
        int limit = cy >= top() ? top() : 0;
        cy = std::max(cy - std::max(n, 1), limit);
    }
    void cursor_down(int n) {
        int limit = cy <= bottom() ? bottom() : rows - 1;
        cy = std::min(cy + std::max(n, 1), limit);
    }
    void cursor_back(int n) {
        if (cx == cols) cx--;
        cx -= std::max(n, 1);
        clamp_x();
    }
    void cursor_forward(int n) { cx += std::max(n, 1); clamp_x(); }
    void cursor_position(int line, int column) {
        int y = std::max(line, 1) - 1, x = std::max(column, 1) - 1;
        if (mtop >= 0 && origin) {
            y += mtop;
            if (y < mtop || y > mbot) return;
        }
        cx = x; cy = y;
        clamp_x(); clamp_y();
    }
    void cursor_to_line(int line) {
        cy = std::max(line, 1) - 1;
        if (origin && mtop >= 0) cy += mtop;
        clamp_y();
    }
    void tab(int n) {
        for (int i = 0; i < std::max(n, 1); i++) {
            int x = cx + 1;
            while (x < cols && !tabs[x]) x++;
            cx = std::min(x, cols - 1);
        }
    }
    void back_tab(int n) {
        for (int i = 0; i < std::max(n, 1); i++) {
            int x = std::min(cx, cols) - 1;
            while (x > 0 && !tabs[x]) x--;
            cx = std::max(x, 0);
        }
    }
    void save_cursor() { saved = Saved{cx, cy, pen, origin, autowrap, true}; }
    void restore_cursor() {
        if (!saved.valid) {
            origin = false;
            cursor_position(1, 1);
            return;
        }
        cx = saved.x; cy = saved.y; pen = saved.pen;
        origin = saved.origin; autowrap = saved.wrap;
        clamp_x();
        cy = std::min(std::max(cy, 0), rows - 1);
    }

    // ---- scrolling ----
    void scroll_up(int t, int b) {         // the region's top line leaves (into history, as pyte), a blank one enters below
        push_history(std::move(buf[t]));
        for (int y = t; y < b; y++) buf[y] = std::move(buf[y + 1]);
        buf[b] = Line();
        mark_all();
    }
    void scroll_down(int t, int b) {
        for (int y = b; y > t; y--) buf[y] = std::move(buf[y - 1]);
        buf[t] = Line();
        mark_all();
    }
    void index() {
        if (cy == bottom()) scroll_up(top(), bottom());
        else cursor_down(1);
    }
    void reverse_index() {
        if (cy == top()) scroll_down(top(), bottom());
        else cursor_up(1);
    }
    void linefeed() {
        index();
        if (newline_mode) cx = 0;
        buf[cy].cont = drawing;
    }

    // ---- drawing ----
    static bool is_stub(const Cell &c) { return (c.ch & (CP_MASK | INTERNED)) == STUB; }
    static void blank(Cell &c) { c.ch = (c.ch & ATTR_MASK) | ' '; }
    // Cells x..last are about to be overwritten: a wide character cut in half loses its other half
    static void split_wide(Line &l, int x, int last) {
        int n = l.cells.size();
        if (x > 0 && x < n && is_stub(l.cells[x])) blank(l.cells[x - 1]);
        if (last + 1 < n && is_stub(l.cells[last + 1])) blank(l.cells[last + 1]);
    }
    void combine(uint32_t cp) {
        int x = std::min(cx, cols) - 1, y = cy;
        if (x < 0) {
            if (y == 0) return;
            y--; x = cols - 1;
        }
        Line &l = buf[y];
        if (x < int(l.cells.size()) && is_stub(l.cells[x]) && x > 0) x--;
        Cell &c = at(l, x);
        std::string s;
        cell_text(s, c.ch);
        put_utf8(s, cp);
        c.ch = (c.ch & ATTR_MASK) | intern(s);
        mark(y);
    }
    void draw(uint32_t cp) {
        int w = cp_width(cp);
        if (w < 0) return;                       // a control character: not drawn
        if (w == 0) { combine(cp); return; }
        if (cx >= cols || (w == 2 && cx == cols - 1 && cols > 1)) {
            if (autowrap) {
                mark(cy);
                cx = 0;
                drawing = true;
                linefeed();
                drawing = false;
            } else {
                cx = std::max(cols - w, 0);
            }
        }
        if (insert_mode) insert_characters(w);
        Line &l = buf[cy];
        bool two = w == 2 && cx + 1 < cols;
        split_wide(l, cx, two ? cx + 1 : cx);
        at(l, cx) = pen.cell(cp);
        if (two) at(l, cx + 1) = pen.cell(STUB);
        cx = std::min(cx + w, cols);
        last_cp = cp;
        mark(cy);
    }

    // ---- editing ----
    void insert_characters(int n) {
        n = std::max(n, 1);
        mark(cy);
        Line &l = buf[cy];
        if (cx >= int(l.cells.size()) || cx >= cols) return;
        if (int(l.cells.size()) > cols) l.cells.resize(cols);
        l.cells.insert(l.cells.begin() + cx, std::min(n, cols - cx), BLANK);
        if (int(l.cells.size()) > cols) l.cells.resize(cols);
    }
    void delete_characters(int n) {
        n = std::max(n, 1);
        mark(cy);
        Line &l = buf[cy];
        int size = l.cells.size();
        if (cx >= size) return;
        l.cells.erase(l.cells.begin() + cx, l.cells.begin() + std::min(cx + n, size));
    }
    void erase_characters(int n) {
        n = std::max(n, 1);
        mark(cy);
        Line &l = buf[cy];
        for (int x = cx; x < std::min(cx + n, cols); x++) at(l, x) = pen.cell(' ');
    }
    void erase_in_line(int how) {
        int from, to;
        if (how == 0) { from = cx; to = cols; }
        else if (how == 1) { from = 0; to = std::min(cx + 1, cols); }
        else if (how == 2) { from = 0; to = cols; }
        else return;
        mark(cy);
        Line &l = buf[cy];
        for (int x = from; x < to; x++) at(l, x) = pen.cell(' ');
    }
    void erase_in_display(int how) {
        int from, to;
        if (how == 0) { from = cy + 1; to = rows; }
        else if (how == 1) { from = 0; to = cy; }
        else if (how == 2 || how == 3) { from = 0; to = rows; }
        else return;
        for (int y = from; y < to; y++) {
            mark(y);
            for (Cell &c : buf[y].cells) c = pen.cell(' ');     // only what the row has (like pyte)
        }
        if (how == 0 || how == 1) erase_in_line(how);
        if (how == 3) clear_history();
        int a = how == 0 ? cy + 1 : 0, b = how == 1 ? cy + 1 : rows;
        for (int y = a; y < b; y++) buf[y].cont = false;
    }
    void insert_lines(int n) {
        n = std::max(n, 1);
        int t = top(), b = bottom();
        if (cy < t || cy > b) return;
        mark_from(cy);
        for (int y = b; y >= cy; y--) {
            if (y + n <= b) buf[y + n] = std::move(buf[y]);
            buf[y] = Line();
        }
        cx = 0;
    }
    void delete_lines(int n) {
        n = std::max(n, 1);
        int t = top(), b = bottom();
        if (cy < t || cy > b) return;
        mark_from(cy);
        for (int y = cy; y <= b; y++) {
            if (y + n <= b) buf[y] = std::move(buf[y + n]);
            else buf[y] = Line();
        }
        cx = 0;
    }
    void alignment_display() {
        mark_all();
        for (auto &l : buf)
            for (int x = 0; x < cols; x++) {
                Cell &c = at(l, x);
                c.ch = (c.ch & ATTR_MASK) | 'E';
            }
    }
    void set_margins(int t, int b) {
        if (t == 0 && b == 0) {
            mtop = mbot = -1;
        } else {
            t = t ? t - 1 : 0;
            b = b ? b - 1 : rows - 1;
            t = std::min(std::max(t, 0), rows - 1);
            b = std::min(std::max(b, 0), rows - 1);
            if (b - t < 1) return;
            mtop = t; mbot = b;
        }
        cursor_position(1, 1);
    }
    void reset() {
        mark_all();
        for (auto &l : buf) l = Line();
        mtop = mbot = -1;
        autowrap = true; insert_mode = newline_mode = origin = false;
        reset_tabs(0);
        cx = cy = 0;
        pen = Pen();
        clear_history();
    }
    void resize(int new_rows, int new_cols) {
        if (new_rows <= 0) new_rows = rows;
        if (new_cols <= 0) new_cols = cols;
        if (new_rows == rows && new_cols == cols) return;
        if (new_rows < rows) {
            int drop = rows - new_rows;
            // blank rows below the cursor go first, then the top rows scroll into history
            while (drop > 0 && int(buf.size()) - 1 > cy) {
                const Line &last = buf.back();
                if (std::any_of(last.cells.begin(), last.cells.end(), [](const Cell &c) { return !(c == BLANK); }))
                    break;
                buf.pop_back();
                drop--;
            }
            for (int i = 0; i < drop; i++) push_history(std::move(buf[i]));
            buf.erase(buf.begin(), buf.begin() + drop);
            cy = std::max(cy - drop, 0);
        }
        buf.resize(new_rows);
        if (new_cols < cols)
            for (auto &l : buf)
                if (int(l.cells.size()) > new_cols) l.cells.resize(new_cols);
        int old_cols = cols;
        rows = new_rows; cols = new_cols;
        if (cols > old_cols) reset_tabs(old_cols);
        else tabs.resize(cols);
        dirty.assign(rows, 1);
        mtop = mbot = -1;
        cx = std::min(cx, cols);
        cy = std::min(cy, rows - 1);
    }

    // ---- modes and SGR ----
    void set_modes(bool on) {
        for (size_t i = 0; i < params.size(); i++) {
            int m = params[i];
            if (priv) {
                if (m == 6) { origin = on; cursor_position(1, 1); }
                else if (m == 7) autowrap = on;
            } else {
                if (m == 4) insert_mode = on;
                else if (m == 20) newline_mode = on;
            }
        }
    }
    uint32_t colour_after(size_t &i, bool colon) {
        // params[i] is 38/48/58; returns the colour (or ~0u for none), leaves i on the last used
        size_t n = params.size();
        auto more = [&](size_t j) { return j < n && (!colon || sub[j]); };
        if (!more(i + 1)) return ~0u;
        int kind = params[i + 1];
        if (kind == 5) {
            if (!more(i + 2)) { i += 1; return ~0u; }
            int c = params[i + 2];
            i += 2;
            return palette256(std::min(std::max(c, 0), 255));
        }
        if (kind == 2) {
            size_t first = i + 2;
            if (colon) {                         // 38:2:<colourspace>:r:g:b, colourspace optional
                size_t last = i + 1;
                while (more(last + 1)) last++;
                if (last - (i + 1) >= 4) first = i + 3;
            }
            if (!more(first + 2)) { i = n; return ~0u; }
            uint32_t rgb = RGB_FLAG | (params[first] & 0xFF) << 16 | (params[first + 1] & 0xFF) << 8 |
                           (params[first + 2] & 0xFF);
            i = first + 2;
            return rgb;
        }
        i += 1;
        return ~0u;
    }
    void sgr() {
        if (priv || priv_other) return;
        if (params.empty()) params.push_back(0);
        for (size_t i = 0; i < params.size(); i++) {
            int a = params[i];
            bool colon = i + 1 < params.size() && sub[i + 1];
            if (a == 38 || a == 48 || a == 58) {
                uint32_t c = colour_after(i, colon);
                if (c != ~0u) {
                    if (a == 38) pen.fg = c;
                    else if (a == 48) pen.bg = c;
                }
                continue;
            }
            if (colon) {                          // 4:x underline styles; other sub-params: skip
                if (a == 4) {
                    if (params[i + 1]) pen.flags |= UNDER; else pen.flags &= ~UNDER;
                }
                while (i + 1 < params.size() && sub[i + 1]) i++;
                continue;
            }
            switch (a) {
            case 0: pen = Pen(); break;
            case 1: pen.flags |= BOLD; break;
            case 2: pen.flags |= DIM; break;     // pyte drops dim; the service shows it (hints, ghost text)
            case 3: pen.flags |= ITALIC; break;
            case 4: pen.flags |= UNDER; break;
            case 7: pen.flags |= REVERSE; break;
            case 22: pen.flags &= ~(BOLD | DIM); break;
            case 23: pen.flags &= ~ITALIC; break;
            case 24: pen.flags &= ~UNDER; break;
            case 27: pen.flags &= ~REVERSE; break;
            case 39: pen.fg = 0; break;
            case 49: pen.bg = 0; break;
            default:
                if (a >= 30 && a <= 37) pen.fg = a - 30 + 1;
                else if (a >= 40 && a <= 47) pen.bg = a - 40 + 1;
                else if (a >= 90 && a <= 97) pen.fg = a - 90 + 9;
                else if (a >= 100 && a <= 107) pen.bg = a - 100 + 9;
            }
        }
    }

    // ---- parser ----
    int p(size_t i, int def = 0) const { return i < params.size() ? params[i] : def; }
    void csi_dispatch(uint32_t f) {
        if (inter || priv_other) return;      // ESC[2 q, ESC[>4;2m ...: nothing on screen
        if (params.empty()) params.push_back(0);
        if (priv) {
            if (f == 'h') set_modes(true);
            else if (f == 'l') set_modes(false);
            else if (f == 'J') erase_in_display(p(0));
            else if (f == 'K') erase_in_line(p(0));
            return;
        }
        int a = p(0);
        switch (f) {
        case '@': insert_characters(a); break;
        case 'A': cursor_up(a); break;
        case 'B': case 'e': cursor_down(a); break;
        case 'C': case 'a': cursor_forward(a); break;
        case 'D': cursor_back(a); break;
        case 'E': cursor_down(a); cx = 0; break;
        case 'F': cursor_up(a); cx = 0; break;
        case 'G': case '`': cx = std::max(a, 1) - 1; clamp_x(); break;
        case 'H': case 'f': cursor_position(a, p(1)); break;
        case 'I': tab(a); break;
        case 'J': erase_in_display(a); break;
        case 'K': erase_in_line(a); break;
        case 'L': insert_lines(a); break;
        case 'M': delete_lines(a); break;
        case 'P': delete_characters(a); break;
        case 'S': for (int i = 0; i < std::min(std::max(a, 1), rows); i++) scroll_up(top(), bottom()); break;
        case 'T': for (int i = 0; i < std::min(std::max(a, 1), rows); i++) scroll_down(top(), bottom()); break;
        case 'X': erase_characters(a); break;
        case 'Z': back_tab(a); break;
        case 'b':
            if (last_cp) for (int i = 0; i < std::min(std::max(a, 1), 65535); i++) draw(last_cp);
            break;
        case 'd': cursor_to_line(a); break;
        case 'g':
            if (a == 0) { if (cx < cols) tabs[cx] = 0; }
            else if (a == 3) std::fill(tabs.begin(), tabs.end(), 0);
            break;
        case 'h': set_modes(true); break;
        case 'l': set_modes(false); break;
        case 'm': sgr(); break;
        case 'r': set_margins(p(0), p(1)); break;
        case 's': save_cursor(); break;
        case 'u': restore_cursor(); break;
        default: break;                       // DA, DSR, window ops ...: nothing to draw
        }
    }
    void esc_dispatch(uint32_t f) {
        switch (f) {
        case 'c': reset(); break;
        case 'D': index(); break;
        case 'E': linefeed(); cx = 0; break;
        case 'M': reverse_index(); break;
        case 'H': if (cx < cols) tabs[cx] = 1; break;
        case '7': save_cursor(); break;
        case '8': restore_cursor(); break;
        default: break;
        }
    }
    bool control(uint32_t c) {               // C0 controls that act anywhere; true if handled
        switch (c) {
        case 0x08: cursor_back(1); return true;
        case 0x09: tab(1); return true;
        case 0x0a: case 0x0b: case 0x0c: linefeed(); return true;
        case 0x0d: cx = 0; return true;
        case 0x07: case 0x00: case 0x0e: case 0x0f: return true;
        default: return false;
        }
    }
    void start_csi() {
        state = CSI_PARAM;
        params.clear(); sub.clear();
        have_param = priv = priv_other = inter = false;
    }
    void put(uint32_t c) {
        switch (state) {
        case GROUND:
            if (c >= 0x20 && c != 0x7f && (c < 0x80 || c > 0x9f)) { draw(c); return; }
            if (c == 0x1b) { state = ESCAPE; return; }
            if (c == 0x9b) { start_csi(); return; }
            if (c == 0x9d) { state = OSC_STRING; return; }
            control(c);
            return;
        case ESCAPE:
            if (c == 0x1b) return;
            if (c < 0x20) { control(c); return; }
            if (c == '[') { start_csi(); return; }
            if (c == ']') { state = OSC_STRING; return; }
            if (c == 'P' || c == 'X' || c == '^' || c == '_') { state = SKIP_STRING; return; }
            if (c >= 0x20 && c <= 0x2f) { esc_inter = char(c); state = ESC_INTER; return; }
            state = GROUND;
            esc_dispatch(c);
            return;
        case ESC_INTER:
            if (c == 0x1b) { state = ESCAPE; return; }
            if (c < 0x20) { control(c); return; }
            if (c >= 0x20 && c <= 0x2f) return;
            state = GROUND;
            if (esc_inter == '#' && c == '8') alignment_display();
            return;                            // charset choices (ESC ( B ...): UTF-8 only
        case CSI_PARAM:
            if (c >= '0' && c <= '9') {
                if (!have_param) { params.push_back(0); sub.push_back(0); have_param = true; }
                int &v = params.back();
                v = std::min(v * 10 + int(c - '0'), 9999);
                return;
            }
            if (c == ';' || c == ':') {
                if (!have_param) { params.push_back(0); sub.push_back(0); }
                if (params.size() < 64) { params.push_back(0); sub.push_back(c == ':'); }
                have_param = true;
                return;
            }
            if (c == '?') { priv = true; return; }
            if (c == '<' || c == '=' || c == '>') { priv_other = true; return; }
            if (c >= 0x20 && c <= 0x2f) { inter = true; return; }
            if (c >= 0x40 && c <= 0x7e) { state = GROUND; csi_dispatch(c); return; }
            if (c == 0x1b) { state = ESCAPE; return; }
            if (c == 0x18 || c == 0x1a) { state = GROUND; return; }
            if (c < 0x20) { control(c); return; }
            state = GROUND;                    // anything else: drop the sequence
            return;
        case OSC_STRING:                       // titles, cwd, hyperlinks, shell marks: nothing to draw
            if (c == 0x07 || c == 0x9c || c == 0x18 || c == 0x1a) state = GROUND;
            else if (c == 0x1b) state = OSC_ESC;
            return;
        case SKIP_STRING:
            if (c == 0x9c || c == 0x18 || c == 0x1a) state = GROUND;
            else if (c == 0x1b) state = SKIP_ESC;
            return;
        case OSC_ESC:
        case SKIP_ESC:
            if (c == '\\') { state = GROUND; return; }
            state = ESCAPE;                    // an unterminated string: start over with this escape
            put(c);
            return;
        }
    }
    void feed(const unsigned char *s, size_t n) {
        for (size_t i = 0; i < n; i++) {
            unsigned char b = s[i];
            if (utf8_need) {
                if ((b & 0xC0) == 0x80) {
                    utf8_cp = utf8_cp << 6 | (b & 0x3F);
                    if (--utf8_need == 0) put(utf8_cp);
                    continue;
                }
                utf8_need = 0;
                put(0xFFFD);
            }
            if (b < 0x80) {
                if (state == GROUND && b >= 0x20 && b < 0x7f) { draw(b); continue; }
                put(b);
            }
            else if ((b & 0xE0) == 0xC0) { utf8_cp = b & 0x1F; utf8_need = 1; }
            else if ((b & 0xF0) == 0xE0) { utf8_cp = b & 0x0F; utf8_need = 2; }
            else if ((b & 0xF8) == 0xF0) { utf8_cp = b & 0x07; utf8_need = 3; }
            else put(0xFFFD);
        }
    }

    // ---- rendering ----
    static void sgr_colour(std::string &s, uint32_t c, bool bg) {
        if (!c) return;
        s += ';';
        if (c & RGB_FLAG) {
            s += bg ? "48;2;" : "38;2;";
            s += std::to_string(c >> 16 & 0xFF); s += ';';
            s += std::to_string(c >> 8 & 0xFF); s += ';';
            s += std::to_string(c & 0xFF);
        } else {
            int k = int(c) - 1;
            s += std::to_string(k < 8 ? (bg ? 40 : 30) + k : (bg ? 100 : 90) + k - 8);
        }
    }
    // One row -> plain text and text with ANSI colours, trailing blanks dropped; blanks that
    // only carry a background colour are dropped too, all but one (see orbitd.py: _render_row).
    void render(const Line &l, std::string &plain, std::string &ansi) const {
        int n = std::min(int(l.cells.size()), cols);
        size_t last_text = 0, pad_end = 0, last_plain = 0;
        bool have = false;
        uint32_t cur_flags = 0, cur_fg = 0, cur_bg = 0;
        for (int x = 0; x < n; x++) {
            const Cell &c = l.cells[x];
            if (is_stub(c)) continue;
            uint32_t flags = c.ch & ATTR_MASK;
            if (!have || flags != cur_flags || c.fg != cur_fg || c.bg != cur_bg) {
                have = true;
                cur_flags = flags; cur_fg = c.fg; cur_bg = c.bg;
                ansi += "\x1b[0";
                sgr_colour(ansi, c.fg, false);
                sgr_colour(ansi, c.bg, true);
                if (flags & BOLD) ansi += ";1";
                if (flags & DIM) ansi += ";2";
                if (flags & ITALIC) ansi += ";3";
                if (flags & UNDER) ansi += ";4";
                if (flags & REVERSE) ansi += ";7";
                ansi += 'm';
            }
            bool blank = c.ch == (' ' | flags);
            cell_text(ansi, c.ch);
            cell_text(plain, c.ch);
            if (!blank) last_plain = plain.size();
            if (!blank || (flags & REVERSE)) last_text = pad_end = ansi.size();
            else if (c.bg && pad_end == last_text) pad_end = ansi.size();
        }
        ansi.resize(std::max(last_text, pad_end));
        if (!ansi.empty()) ansi += "\x1b[0m";
        plain.resize(last_plain);
    }
    // rows [a, b) of the screen (which 0) or history (which 1), as plain \x1f ansi \x1f cont \x1e
    const std::string &rows_text(int which, long a, long b) {
        out.clear();
        long n = which ? long(history.size()) : long(rows);
        a = std::max(a, 0L); b = std::min(b, n);
        for (long i = a; i < b; i++) {
            const Line &l = which ? history[i] : buf[i];
            std::string plain, ansi;
            render(l, plain, ansi);
            out += plain; out += '\x1f'; out += ansi; out += '\x1f';
            out += l.cont ? '1' : '0';
            out += '\x1e';
        }
        return out;
    }
    // every cell of row y up to the width (blanks included, second halves of wide characters
    // left out): u32 count, u16 x each, u8 dim each, then the texts joined by \x1e
    const std::string &cells_text(int y) {
        out.clear();
        if (y < 0 || y >= rows) return out;
        const Line &l = buf[y];
        std::vector<uint16_t> xs;
        std::string dims, texts;
        for (int x = 0; x < cols; x++) {
            Cell c = x < int(l.cells.size()) ? l.cells[x] : BLANK;
            if (is_stub(c)) continue;
            if (!xs.empty()) texts += '\x1e';
            xs.push_back(x);
            dims += (c.ch & DIM) ? '\1' : '\0';
            cell_text(texts, c.ch);
        }
        uint32_t n = xs.size();
        out.append(reinterpret_cast<const char *>(&n), 4);
        out.append(reinterpret_cast<const char *>(xs.data()), 2 * n);
        out += dims;
        out += texts;
        return out;
    }
};

}  // namespace

#define API extern "C" __attribute__((visibility("default")))

API int ovt_abi() { return 1; }
API void *ovt_new(int cols, int rows, int history) { return new Screen(cols, rows, history); }
API void ovt_free(void *s) { delete static_cast<Screen *>(s); }
API void ovt_feed(void *s, const char *data, size_t n) {
    static_cast<Screen *>(s)->feed(reinterpret_cast<const unsigned char *>(data), n);
}
API void ovt_resize(void *s, int rows, int cols) { static_cast<Screen *>(s)->resize(rows, cols); }
API int ovt_columns(void *s) { return static_cast<Screen *>(s)->cols; }
API int ovt_lines(void *s) { return static_cast<Screen *>(s)->rows; }
API int ovt_cursor_x(void *s) { return static_cast<Screen *>(s)->cx; }
API int ovt_cursor_y(void *s) { return static_cast<Screen *>(s)->cy; }
API long ovt_history_len(void *s) { return static_cast<Screen *>(s)->history.size(); }
API unsigned long long ovt_history_serial(void *s) { return static_cast<Screen *>(s)->history_serial; }
API int ovt_take_dirty(void *s, int *ys, int max) {
    Screen *scr = static_cast<Screen *>(s);
    int n = 0;
    for (int y = 0; y < scr->rows; y++)
        if (scr->dirty[y]) {
            if (n < max) ys[n++] = y;
            scr->dirty[y] = 0;
        }
    return n;
}
API const char *ovt_rows(void *s, int which, long a, long b, size_t *len) {
    const std::string &o = static_cast<Screen *>(s)->rows_text(which, a, b);
    *len = o.size();
    return o.data();
}
API const char *ovt_cells(void *s, int y, size_t *len) {
    const std::string &o = static_cast<Screen *>(s)->cells_text(y);
    *len = o.size();
    return o.data();
}
