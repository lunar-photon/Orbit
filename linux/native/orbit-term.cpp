// orbit-term: run a shell in a terminal the Orbit phone app can follow.
//
//     orbit-term [command ...]        (default: $SHELL)
//
// Like `script`, it sits between the terminal app and the shell: everything the shell prints
// goes to the terminal unchanged and is also recorded (in `script`'s "advanced" format) to
// ~/.cache/orbit/term/<pid>.{log,timing}, which the Orbit service replays for the phone.
// Unlike `script`, it also takes input from the phone through a socket next to the recording
// (<pid>.sock, only this user can open it), so typing from the phone works in any terminal
// app, not only Konsole. Started by the shell's own startup file, it replaces that shell
// (exec), so the terminal app sees this process where the shell was.
//
// The C++ build of linux/orbit-term (same behaviour and files): it stays in every open
// terminal, so it should be small: ~1 MB of memory instead of ~20 MB for Python.

#include <algorithm>
#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <vector>

#include <fcntl.h>
#include <poll.h>
#include <pty.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

namespace {

volatile sig_atomic_t g_resized = 0, g_stop = 0;
int g_wake[2] = {-1, -1};

void on_signal(int sig) {
    int saved = errno;
    if (sig == SIGWINCH) g_resized = 1;
    else g_stop = sig;
    char c = 0;
    (void)!write(g_wake[1], &c, 1);
    errno = saved;
}

double now() {
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

bool write_all(int fd, const char *p, size_t n) {
    while (n) {
        ssize_t w = write(fd, p, n);
        if (w < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN) {               // a non-blocking terminal that's full: wait
                pollfd pf{fd, POLLOUT, 0};
                poll(&pf, 1, 1000);
                continue;
            }
            return false;
        }
        p += w;
        n -= size_t(w);
    }
    return true;
}

void get_size(int fd, int &rows, int &cols) {
    struct winsize ws{};
    if (ioctl(fd, TIOCGWINSZ, &ws) == 0 && ws.ws_row && ws.ws_col) {
        rows = ws.ws_row;
        cols = ws.ws_col;
    } else {
        rows = 24;
        cols = 80;
    }
}

void set_size(int fd, int rows, int cols) {
    struct winsize ws{};
    ws.ws_row = rows;
    ws.ws_col = cols;
    ioctl(fd, TIOCSWINSZ, &ws);
}

// Take the name and folder of what runs in the terminal (fish, claude, vim ...). The terminal
// app only sees this process, so that's what it names the tab after ("Downloads : claude").
void follow_foreground(int master) {
    pid_t pgrp = tcgetpgrp(master);
    if (pgrp <= 0) return;
    char path[64], name[64] = {0}, cwd[4096];
    snprintf(path, sizeof path, "/proc/%d/comm", int(pgrp));
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return;
    ssize_t n = read(fd, name, sizeof name - 1);
    close(fd);
    if (n <= 0) return;
    while (n && (name[n - 1] == '\n' || name[n - 1] == ' ')) name[--n] = 0;
    snprintf(path, sizeof path, "/proc/%d/cwd", int(pgrp));
    ssize_t m = readlink(path, cwd, sizeof cwd - 1);
    if (m <= 0) return;
    cwd[m] = 0;
    name[15] = 0;
    prctl(PR_SET_NAME, name, 0, 0, 0);
    (void)!chdir(cwd);
}

bool is_digit(char c) { return c >= '0' && c <= '9'; }

// Inputs that only scroll an app's own view (Claude Code's fullscreen mode): the mouse wheel
// (SGR and X10 reports, button 64/65 plus modifier bits), Page Up/Down and Ctrl+Home/End. Noting
// them in the timing file lets the replay tell "you scrolled" from "new text arrived" for
// certain. Only the kind of input is written, never what was typed.
void scroll_inputs(const char *d, size_t n, std::vector<const char *> &found) {
    for (size_t i = 0; i + 2 < n; i++) {
        if (d[i] != '\x1b' || d[i + 1] != '[') continue;
        size_t j = i + 2;
        if (d[j] == '<') {                                    // ESC [ < b ; x ; y M/m
            size_t k = j + 1, start = k;
            int b = 0;
            while (k < n && is_digit(d[k])) b = std::min(b * 10 + (d[k++] - '0'), 100000);
            if (k == start) continue;
            bool ok = true;
            for (int part = 0; part < 2 && ok; part++) {
                if (k >= n || d[k] != ';') { ok = false; break; }
                size_t s = ++k;
                while (k < n && is_digit(d[k])) k++;
                if (k == s) ok = false;
            }
            if (!ok || k >= n || (d[k] != 'M' && d[k] != 'm')) continue;
            b &= ~(4 | 8 | 16);                              // drop shift/alt/ctrl bits
            if (b == 64 || b == 65) found.push_back(b == 64 ? "up" : "down");
            i = k;
        } else if (d[j] == 'M') {                             // ESC [ M b x y, each byte >= 0x20
            if (j + 3 >= n) continue;
            unsigned char b0 = d[j + 1], b1 = d[j + 2], b2 = d[j + 3];
            if (b0 < 0x20 || b1 < 0x20 || b2 < 0x20) continue;
            int b = (b0 - 32) & ~(4 | 8 | 16);
            if (b == 64 || b == 65) found.push_back(b == 64 ? "up" : "down");
            i = j + 3;
        } else if (d[j] == '5' || d[j] == '6') {              // ESC [ 5 ~, ESC [ 5 ; m ~
            size_t k = j + 1;
            if (k < n && d[k] == ';') {
                size_t s = ++k;
                while (k < n && is_digit(d[k])) k++;
                if (k == s) continue;
            }
            if (k < n && d[k] == '~') {
                found.push_back(d[j] == '5' ? "up" : "down");
                i = k;
            }
        } else if (d[j] == '1' && j + 1 < n && d[j + 1] == ';') {   // ESC [ 1 ; m H / F
            size_t k = j + 2, s = k;
            while (k < n && is_digit(d[k])) k++;
            if (k == s || k >= n) continue;
            if (d[k] == 'H' || d[k] == 'F') {
                found.push_back(d[k] == 'H' ? "top" : "bottom");
                i = k;
            }
        }
    }
}

std::string json_str(const std::string &s) {
    std::string o = "\"";
    for (unsigned char c : s) {
        if (c == '"' || c == '\\') { o += '\\'; o += char(c); }
        else if (c < 0x20) {
            char b[8];
            snprintf(b, sizeof b, "\\u%04x", c);
            o += b;
        } else o += char(c);
    }
    return o + "\"";
}

std::string json_num(double v) {
    char b[64];
    snprintf(b, sizeof b, "%.6f", v);
    return b;
}

}  // namespace

int main(int argc, char **argv) {
    std::vector<std::string> cmd(argv + 1, argv + argc);
    if (cmd.empty()) {
        const char *sh = getenv("SHELL");
        cmd.push_back(sh && *sh ? sh : "/bin/sh");
    }
    std::vector<char *> cargv;
    for (auto &s : cmd) cargv.push_back(const_cast<char *>(s.c_str()));
    cargv.push_back(nullptr);

    if (!isatty(0) || !isatty(1)) {                 // nothing to sit between: just run it
        execvp(cargv[0], cargv.data());
        perror(cargv[0]);
        return 127;
    }

    const char *home = getenv("HOME");
    std::string dir = std::string(home ? home : "") + "/.cache/orbit/term";
    {
        std::string parent = std::string(home ? home : "") + "/.cache";
        mkdir(parent.c_str(), 0700);
        mkdir((parent + "/orbit").c_str(), 0700);
        mkdir(dir.c_str(), 0700);
    }
    std::string base = dir + "/" + std::to_string(getpid());
    int rows, cols;
    get_size(0, rows, cols);

    termios attrs{};
    tcgetattr(0, &attrs);
    int master = -1, slave = -1;
    struct winsize ws{};
    ws.ws_row = rows;
    ws.ws_col = cols;
    if (openpty(&master, &slave, nullptr, &attrs, &ws) < 0) {   // the right size before the shell starts
        perror("orbit-term: openpty");
        execvp(cargv[0], cargv.data());
        return 127;
    }

    pid_t child = fork();
    if (child == 0) {
        close(master);
        setsid();
        ioctl(slave, TIOCSCTTY, 0);
        for (int fd = 0; fd < 3; fd++) dup2(slave, fd);
        if (slave > 2) close(slave);
        setenv("ORBIT_RECORDING", base.c_str(), 1);
        signal(SIGPIPE, SIG_DFL);
        execvp(cargv[0], cargv.data());
        perror(cargv[0]);
        _exit(127);
    }
    close(slave);
    fcntl(master, F_SETFD, FD_CLOEXEC);
    signal(SIGPIPE, SIG_IGN);

    int log = open((base + ".log").c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    int timing = open((base + ".timing").c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    fchmod(log, 0600);
    fchmod(timing, 0600);
    auto tline = [&](const std::string &s) {
        if (timing >= 0) write_all(timing, s.data(), s.size());
    };
    tline("H 0.000000 COLUMNS " + std::to_string(cols) + "\nH 0.000000 LINES " + std::to_string(rows) +
          "\nH 0.000000 ORBIT_INPUT_MARKS 1\n");
    {
        const char *tp = getenv("TERM_PROGRAM");
        std::string app = tp && *tp ? tp : (getenv("KONSOLE_VERSION") ? "konsole" : "");
        const char *tty = ttyname(0);
        timespec wall;
        clock_gettime(CLOCK_REALTIME, &wall);
        std::string meta = "{\"pid\": " + std::to_string(getpid()) + ", \"shell_pid\": " + std::to_string(child) +
                           ", \"command\": [";
        for (size_t i = 0; i < cmd.size(); i++) meta += (i ? ", " : "") + json_str(cmd[i]);
        meta += "], \"app\": " + json_str(app) + ", \"tty\": " + json_str(tty ? tty : "") +
                ", \"started\": " + json_num(wall.tv_sec + wall.tv_nsec / 1e9) + "}";
        int fd = open((base + ".json").c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
        if (fd >= 0) {
            write_all(fd, meta.data(), meta.size());
            fchmod(fd, 0600);
            close(fd);
        }
    }

    // input from the phone
    std::string sock_path = base + ".sock";
    unlink(sock_path.c_str());
    int server = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (server >= 0) {
        sockaddr_un addr{};
        addr.sun_family = AF_UNIX;
        strncpy(addr.sun_path, sock_path.c_str(), sizeof addr.sun_path - 1);
        mode_t old = umask(0177);
        if (bind(server, reinterpret_cast<sockaddr *>(&addr), sizeof addr) < 0 || listen(server, 4) < 0) {
            close(server);
            server = -1;
        }
        umask(old);
    }
    std::vector<int> clients;

    // Signals only set flags; a self-pipe wakes poll()
    if (pipe2(g_wake, O_NONBLOCK | O_CLOEXEC) < 0) g_wake[0] = g_wake[1] = -1;
    struct sigaction sa{};
    sa.sa_handler = on_signal;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGWINCH, &sa, nullptr);
    sigaction(SIGHUP, &sa, nullptr);
    sigaction(SIGTERM, &sa, nullptr);

    double last = now(), followed = 0;
    auto stamp = [&]() {
        double t = now(), d = t - last;
        last = t;
        char b[32];
        snprintf(b, sizeof b, "%.6f", d);
        return std::string(b);
    };

    // Input for the shell is queued and written only when the pty takes it: a big paste from
    // the phone must not block us, or we'd stop reading the shell's output and both would wait
    // on each other forever
    fcntl(master, F_SETFL, fcntl(master, F_GETFL) | O_NONBLOCK);
    std::string pending;

    termios old_tty{}, raw{};
    bool have_tty = tcgetattr(0, &old_tty) == 0;
    if (have_tty) {
        raw = old_tty;
        cfmakeraw(&raw);
        tcsetattr(0, TCSAFLUSH, &raw);
    }

    std::vector<char> buf(65536);
    std::vector<const char *> found;
    while (!g_stop) {
        if (g_resized) {
            g_resized = 0;
            get_size(0, rows, cols);
            set_size(master, rows, cols);                 // the shell gets SIGWINCH from the pty
            tline("S " + stamp() + " SIGWINCH ROWS=" + std::to_string(rows) + " COLS=" + std::to_string(cols) + "\n");
        }
        std::vector<pollfd> fds;
        fds.push_back({g_wake[0], POLLIN, 0});
        fds.push_back({master, short(POLLIN | (pending.empty() ? 0 : POLLOUT)), 0});
        fds.push_back({pending.size() < (1u << 20) ? 0 : -1, POLLIN, 0});   // stop taking input while 1 MB waits
        fds.push_back({server, POLLIN, 0});
        for (int c : clients) fds.push_back({c, POLLIN, 0});
        if (poll(fds.data(), fds.size(), -1) < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (fds[0].revents & POLLIN) {
            char tmp[64];
            while (read(g_wake[0], tmp, sizeof tmp) > 0) {}
        }
        short mr = fds[1].revents;
        if ((mr & POLLOUT) && !pending.empty()) {
            ssize_t w = write(master, pending.data(), std::min<size_t>(pending.size(), 65536));
            if (w > 0) pending.erase(0, size_t(w));
        }
        if (mr & (POLLIN | POLLHUP | POLLERR)) {
            ssize_t n = read(master, buf.data(), buf.size());
            if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) break;   // the shell is gone
            if (n > 0) {
                if (now() - followed > 0.5) {
                    followed = now();
                    follow_foreground(master);
                }
                if (!write_all(1, buf.data(), size_t(n))) break;
                if (log >= 0) write_all(log, buf.data(), size_t(n));
                tline("O " + stamp() + " " + std::to_string(n) + "\n");
            }
        }
        if (fds[2].revents & (POLLIN | POLLHUP | POLLERR)) {
            ssize_t n = read(0, buf.data(), buf.size());
            if (n <= 0 && !(n < 0 && (errno == EINTR || errno == EAGAIN))) break;   // the terminal is gone
            if (n > 0) {
                found.clear();
                scroll_inputs(buf.data(), size_t(n), found);
                for (const char *dir_name : found) tline("X " + stamp() + " SCROLL " + dir_name + "\n");
                pending.append(buf.data(), size_t(n));
            }
        }
        if (server >= 0 && (fds[3].revents & POLLIN)) {
            int c = accept4(server, nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC);
            if (c >= 0) clients.push_back(c);
        }
        for (size_t i = 4; i < fds.size(); i++) {
            if (!(fds[i].revents & (POLLIN | POLLHUP | POLLERR))) continue;
            int c = fds[i].fd;
            ssize_t n = recv(c, buf.data(), buf.size(), 0);
            if (n > 0) pending.append(buf.data(), size_t(n));
            else if (n == 0 || (errno != EAGAIN && errno != EINTR)) {
                close(c);
                clients.erase(std::find(clients.begin(), clients.end(), c));
            }
        }
    }

    if (have_tty) tcsetattr(0, TCSAFLUSH, &old_tty);   // fails harmlessly if the terminal hung up
    for (int c : clients) close(c);
    if (server >= 0) close(server);
    unlink(sock_path.c_str());
    unlink((base + ".json").c_str());
    // hang up the shell's terminal so it (and what runs in it) exits, as closing a tab would
    close(master);
    kill(-child, SIGHUP);
    int status = 0, code = 0;
    double deadline = now() + 3;
    bool killed = false;
    for (;;) {
        pid_t r = waitpid(child, &status, WNOHANG);
        if (r < 0) break;
        if (r == child) {
            code = WIFEXITED(status) ? WEXITSTATUS(status) : WIFSIGNALED(status) ? 128 + WTERMSIG(status) : 0;
            break;
        }
        if (!killed && now() > deadline) {
            kill(-child, SIGKILL);
            killed = true;
        }
        usleep(50000);
    }
    if (g_stop) code = 128 + g_stop;
    return code;
}
