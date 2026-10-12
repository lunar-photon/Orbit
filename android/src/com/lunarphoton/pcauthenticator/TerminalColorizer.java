package com.lunarphoton.pcauthenticator;

import android.graphics.Color;
import android.graphics.Typeface;
import android.text.Spannable;
import android.text.SpannableStringBuilder;
import android.text.style.BackgroundColorSpan;
import android.text.style.ForegroundColorSpan;
import android.text.style.StyleSpan;
import android.text.style.UnderlineSpan;

import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class TerminalColorizer {

    // --- Vibrant High-Contrast Electric Palette (beloved by user from commit 6fa0ad6) ---
    private static int COLOR_DEFAULT = Color.parseColor("#E2E8F0");            // Crisp off-white; replaced by the computer's terminal palette
    private static final int COLOR_MUTED = Color.parseColor("#64748B");        // Slate 500
    private static final int COLOR_DIVIDER = Color.parseColor("#334155");      // Slate 700
    private static final int COLOR_CYAN = Color.parseColor("#38BDF8");         // Sky 400 (Electric Cyan)
    private static final int COLOR_LIGHT_CYAN = Color.parseColor("#7DD3FC");   // Sky 300
    private static final int COLOR_GREEN = Color.parseColor("#34D399");        // Emerald 400
    private static final int COLOR_BRIGHT_GREEN = Color.parseColor("#10B981"); // Emerald 500
    private static final int COLOR_RED = Color.parseColor("#F87171");          // Red 400 (Electric Coral Red)
    private static final int COLOR_YELLOW = Color.parseColor("#FBBF24");       // Amber 400 (Vibrant Gold)
    private static final int COLOR_AMBER = Color.parseColor("#F59E0B");        // Amber 500
    private static final int COLOR_PURPLE = Color.parseColor("#C084FC");       // Purple 400
    private static final int COLOR_VIOLET = Color.parseColor("#A78BFA");       // Violet 400
    private static final int COLOR_WHITE = Color.parseColor("#FFFFFF");        // Pure White
    private static int COLOR_SCREEN_BG = Color.parseColor("#0A0E14");          // Terminal background (for reverse video)
    // The computer's terminal colour scheme (Konsole), so the phone matches what's on screen there
    private static int[] paletteNormal = null;
    private static int[] paletteBright = null;

    /** Use the computer's terminal colours: default text/background and the 16 ANSI colours. */
    public static void setPalette(int fg, int bg, int[] normal, int[] bright) {
        COLOR_DEFAULT = fg;
        COLOR_SCREEN_BG = bg;
        paletteNormal = normal;
        paletteBright = bright;
    }

    private static final Pattern ANSI_PATTERN = Pattern.compile("\u001B\\[([0-9;:]*)m");
    // Non-colour CSI codes (cursor moves etc.). Must not match SGR, which ends in 'm'.
    private static final Pattern ANSI_OTHER_PATTERN = Pattern.compile("\u001B\\[[?0-9;:]*[a-ln-zA-Z]");
    // OSC sequences (titles, hyperlinks) carry no visible text
    private static final Pattern OSC_PATTERN = Pattern.compile("\u001B\\][^\u0007\u001B]*(?:\u0007|\u001B\\\\)");
    // Nerd Font icons live in the Private Use Area; Android fonts render them as boxes
    private static final Pattern PRIVATE_USE_PATTERN = Pattern.compile("[\\uE000-\\uF8FF\\x{F0000}-\\x{10FFFF}]");
    private static final Pattern DIVIDER_PATTERN = Pattern.compile("^[─\\-=*_~]{4,}$");
    private static final Pattern TOOL_PATTERN = Pattern.compile("^[●○]\\s+([A-Za-z0-9_]+)(\\(.*?\\))?(.*)$");
    private static final Pattern THOUGHT_PATTERN = Pattern.compile("^[▸]\\s+(Thought for\\s+)(.*?)$");
    private static final Pattern SPINNER_PATTERN = Pattern.compile("^[⣻⡿⠋⠙⠹]\\s+(.*)$");
    private static final Pattern PROMPT_PATTERN = Pattern.compile("^([>$❯➜])\\s*(.*)$");
    private static final Pattern SHELL_PROMPT_PATTERN = Pattern.compile("^([a-zA-Z0-9_.\\-]+@[a-zA-Z0-9_.\\-]+:[^$#]*[$#])\\s*(.*)$");
    private static final Pattern LIST_ITEM_PATTERN = Pattern.compile("^(\\s*[-•*]\\s+)([^:]+:)(.*)$");
    private static final Pattern NUMBERED_LIST_PATTERN = Pattern.compile("^(\\s*\\d+[.)]\\s+)([^:]+:)(.*)$");
    private static final Pattern MD_HEADING_PATTERN = Pattern.compile("^\\s*#{1,6}\\s+.*$");
    private static final Pattern LABEL_HEADING_PATTERN = Pattern.compile("^\\s*([A-Z][A-Za-z0-9_\\s&\\-/]+:)\\s*$");
    private static final Pattern PATH_OR_CODE_PATTERN = Pattern.compile("(`[^`]+`|~?/[a-zA-Z0-9_.\\-/]+|[a-zA-Z0-9_.\\-]+/[a-zA-Z0-9_.\\-/]+|[a-zA-Z0-9_.\\-]+\\.(?:py|md|java|json|xml|sh|txt|apk|cpp|h|rs|go|js|ts|kt|gradle)|https?://[a-zA-Z0-9_.\\-/]+)");
    private static final Pattern NUMBER_METRIC_PATTERN = Pattern.compile("\\b(\\d+(?:\\.\\d+)?(?:k|ms|s|m|%|lines|tokens|dp|px|sp)?)\\b");
    private static final Pattern BOLD_PATTERN = Pattern.compile("\\*\\*([^*]+)\\*\\*");

    /**
     * Formats terminal text for mobile display:
     * - Trims oversized box-drawing divider lines (137 dashes) down to targetColumns so they never blow out screen width
     * - Strips trailing whitespace on every line so lines that terminate with '...' have ZERO phantom empty space after them
     * - Collapses huge internal whitespace gaps (used by desktop TUIs to right-align items) so footers fit on phone
     */
    public static String formatTerminalText(String text, boolean wrapMode, int targetColumns) {
        if (text == null || text.isEmpty()) return "";

        int maxCols = targetColumns > 20 ? targetColumns : 45;
        StringBuilder sb = new StringBuilder();
        String[] lines = text.split("\n", -1);

        for (int i = 0; i < lines.length; i++) {
            String line = lines[i];

            // Always strip trailing whitespace: eliminates the vast empty space after '...' or short lines
            line = line.replaceAll("\\s+$", "");

            String trimmed = line.trim();

            // Wrapped view only: shorten full-width divider lines so they don't wrap into
            // several rows. Spacing is never changed, so tables and columns stay aligned.
            if (wrapMode && DIVIDER_PATTERN.matcher(trimmed).matches()) {
                char ch = trimmed.charAt(0);
                StringBuilder div = new StringBuilder();
                int count = Math.min(maxCols, 80);
                for (int c = 0; c < count; c++) {
                    div.append(ch);
                }
                line = div.toString();
            }

            sb.append(line);
            if (i < lines.length - 1) {
                sb.append("\n");
            }
        }
        return sb.toString();
    }

    /**
     * Colorizes terminal text into a rich Spannable using vibrant electric colors:
     * - Parses ANSI escape codes if present
     * - Applies full semantic syntax highlighting for active tools, thinking blocks, responses, lists, prompts, and footers
     */
    /** With `ansi`, the text is terminal output with its own colours (a recorded tab): always
     *  read those, even for a chunk that happens to have none, so a few changed lines are
     *  coloured the same way as the whole screen. */
    public static CharSequence colorize(String text, boolean wrapMode, int targetColumns, boolean ansi) {
        if (!ansi) return colorize(text, wrapMode, targetColumns);
        if (text == null || text.isEmpty()) return "";
        text = OSC_PATTERN.matcher(text).replaceAll("");
        text = PRIVATE_USE_PATTERN.matcher(text).replaceAll("");
        return parseAnsiColors(formatTerminalText(text, wrapMode, targetColumns));
    }

    public static CharSequence colorize(String text, boolean wrapMode, int targetColumns) {
        if (text == null || text.isEmpty()) return "";

        text = OSC_PATTERN.matcher(text).replaceAll("");
        text = PRIVATE_USE_PATTERN.matcher(text).replaceAll("");
        String formatted = formatTerminalText(text, wrapMode, targetColumns);

        if (formatted.contains("\u001B[")) {
            return parseAnsiColors(formatted);
        }

        return parseSemanticColors(formatted);
    }

    private static CharSequence parseAnsiColors(String text) {
        String clean = ANSI_OTHER_PATTERN.matcher(text).replaceAll("");

        SpannableStringBuilder ssb = new SpannableStringBuilder();
        Matcher matcher = ANSI_PATTERN.matcher(clean);
        SgrState st = new SgrState();

        int lastEnd = 0;
        while (matcher.find()) {
            int start = matcher.start();
            if (start > lastEnd) {
                appendStyled(ssb, clean.substring(lastEnd, start), st);
            }
            applySgr(st, matcher.group(1));
            lastEnd = matcher.end();
        }

        if (lastEnd < clean.length()) {
            appendStyled(ssb, clean.substring(lastEnd), st);
        }

        return ssb;
    }

    /** Current SGR (Select Graphic Rendition) attributes; null colour = terminal default. */
    private static class SgrState {
        Integer fg, bg;
        boolean bold, dim, italic, underline, inverse;

        void reset() {
            fg = null; bg = null;
            bold = dim = italic = underline = inverse = false;
        }
    }

    private static void applySgr(SgrState st, String params) {
        if (params == null || params.isEmpty()) {
            st.reset();
            return;
        }
        String[] groups = params.split(";", -1);
        for (int i = 0; i < groups.length; i++) {
            String g = groups[i];
            // Colon form (e.g. 38:2::r:g:b or 38:5:n) keeps the whole colour in one group
            if (g.indexOf(':') >= 0) {
                String[] sub = g.split(":", -1);
                int code = parseIntOr(sub[0], -1);
                if ((code == 38 || code == 48) && sub.length >= 3) {
                    Integer c = null;
                    int mode = parseIntOr(sub[1], -1);
                    if (mode == 5) {
                        c = get256Color(parseIntOr(sub[2], 7));
                    } else if (mode == 2 && sub.length >= 5) {
                        int n = sub.length;
                        c = Color.rgb(parseIntOr(sub[n - 3], 0), parseIntOr(sub[n - 2], 0), parseIntOr(sub[n - 1], 0));
                    }
                    if (c != null) {
                        if (code == 38) st.fg = c; else st.bg = c;
                    }
                } else if (code == 4) {
                    st.underline = sub.length < 2 || parseIntOr(sub[1], 1) != 0;
                }
                continue;
            }

            int code = parseIntOr(g, 0);
            if (code == 0) {
                st.reset();
            } else if (code == 1) {
                st.bold = true;
            } else if (code == 2) {
                st.dim = true;
            } else if (code == 3) {
                st.italic = true;
            } else if (code == 4) {
                st.underline = true;
            } else if (code == 7) {
                st.inverse = true;
            } else if (code == 22) {
                st.bold = false;
                st.dim = false;
            } else if (code == 23) {
                st.italic = false;
            } else if (code == 24) {
                st.underline = false;
            } else if (code == 27) {
                st.inverse = false;
            } else if (code >= 30 && code <= 37) {
                st.fg = getAnsiStandardColor(code - 30, false);
            } else if (code == 39) {
                st.fg = null;
            } else if (code >= 40 && code <= 47) {
                st.bg = getAnsiStandardColor(code - 40, false);
            } else if (code == 49) {
                st.bg = null;
            } else if (code >= 90 && code <= 97) {
                st.fg = getAnsiStandardColor(code - 90, true);
            } else if (code >= 100 && code <= 107) {
                st.bg = getAnsiStandardColor(code - 100, true);
            } else if ((code == 38 || code == 48) && i + 1 < groups.length) {
                Integer c = null;
                int mode = parseIntOr(groups[i + 1], -1);
                if (mode == 5 && i + 2 < groups.length) {
                    c = get256Color(parseIntOr(groups[i + 2], 7));
                    i += 2;
                } else if (mode == 2 && i + 4 < groups.length) {
                    c = Color.rgb(parseIntOr(groups[i + 2], 0), parseIntOr(groups[i + 3], 0), parseIntOr(groups[i + 4], 0));
                    i += 4;
                }
                if (c != null) {
                    if (code == 38) st.fg = c; else st.bg = c;
                }
            }
        }
    }

    private static int parseIntOr(String s, int fallback) {
        if (s == null || s.isEmpty()) return fallback;
        try {
            return Integer.parseInt(s);
        } catch (NumberFormatException e) {
            return fallback;
        }
    }

    private static void appendStyled(SpannableStringBuilder ssb, String chunk, SgrState st) {
        if (chunk.isEmpty()) return;
        int s = ssb.length();
        ssb.append(chunk);
        int e = ssb.length();

        int fg = st.fg != null ? st.fg : COLOR_DEFAULT;
        Integer bg = st.bg;
        if (st.inverse) {
            int newFg = bg != null ? bg : COLOR_SCREEN_BG;
            bg = fg;
            fg = newFg;
        }
        if (st.dim) {
            fg = Color.argb(150, Color.red(fg), Color.green(fg), Color.blue(fg));
        }

        ssb.setSpan(new ForegroundColorSpan(fg), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
        if (bg != null) {
            ssb.setSpan(new BackgroundColorSpan(bg), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
        }
        if (st.bold && st.italic) {
            ssb.setSpan(new StyleSpan(Typeface.BOLD_ITALIC), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
        } else if (st.bold) {
            ssb.setSpan(new StyleSpan(Typeface.BOLD), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
        } else if (st.italic) {
            ssb.setSpan(new StyleSpan(Typeface.ITALIC), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
        }
        if (st.underline) {
            ssb.setSpan(new UnderlineSpan(), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
        }
    }

    private static int getAnsiStandardColor(int idx, boolean bright) {
        int[] palette = bright ? paletteBright : paletteNormal;
        if (palette != null && idx >= 0 && idx < palette.length) return palette[idx];
        switch (idx) {
            case 0: return bright ? COLOR_MUTED : COLOR_DIVIDER;
            case 1: return bright ? COLOR_RED : Color.parseColor("#EF4444");
            case 2: return bright ? COLOR_BRIGHT_GREEN : COLOR_GREEN;
            case 3: return bright ? COLOR_YELLOW : COLOR_AMBER;
            case 4: return bright ? COLOR_CYAN : Color.parseColor("#3B82F6");
            case 5: return bright ? COLOR_PURPLE : COLOR_VIOLET;
            case 6: return bright ? COLOR_LIGHT_CYAN : COLOR_CYAN;
            case 7: default: return bright ? COLOR_WHITE : COLOR_DEFAULT;
        }
    }

    private static int get256Color(int idx) {
        if (idx < 16) {
            return getAnsiStandardColor(idx % 8, idx >= 8);
        } else if (idx >= 232) {
            int gray = 8 + (idx - 232) * 10;
            return Color.rgb(gray, gray, gray);
        } else {
            idx -= 16;
            int b = (idx % 6) * 51;
            int g = ((idx / 6) % 6) * 51;
            int r = (idx / 36) * 51;
            return Color.rgb(r, g, b);
        }
    }

    /**
     * Semantic syntax highlighting for plain-text terminal output (Konsole getAllDisplayedText).
     * Works both when terminal is active (tools/spinners) and when idle (assistant response, markdown, lists, prompts).
     */
    private static CharSequence parseSemanticColors(String text) {
        SpannableStringBuilder ssb = new SpannableStringBuilder();
        String[] lines = text.split("\n", -1);

        for (int i = 0; i < lines.length; i++) {
            String line = lines[i];
            int lineStart = ssb.length();
            ssb.append(line);
            int lineEnd = ssb.length();

            colorSingleLine(ssb, line, lineStart, lineEnd);

            if (i < lines.length - 1) {
                ssb.append("\n");
            }
        }
        return ssb;
    }

    private static void colorSingleLine(SpannableStringBuilder ssb, String line, int s, int e) {
        String trimmed = line.trim();
        if (trimmed.isEmpty()) return;

        // 1. Box-drawing divider lines
        if (DIVIDER_PATTERN.matcher(trimmed).matches()) {
            ssb.setSpan(new ForegroundColorSpan(COLOR_DIVIDER), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            return;
        }

        // 2. Active Tool / Agent calls: ● Bash(...), ● Read(...), etc.
        Matcher toolM = TOOL_PATTERN.matcher(trimmed);
        if (toolM.matches()) {
            int relCircle = line.indexOf(trimmed.charAt(0));
            if (relCircle >= 0) {
                ssb.setSpan(new ForegroundColorSpan(COLOR_CYAN), s + relCircle, s + relCircle + 1, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                ssb.setSpan(new StyleSpan(Typeface.BOLD), s + relCircle, s + relCircle + 1, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);

                String toolName = toolM.group(1);
                int relTool = line.indexOf(toolName, relCircle + 1);
                if (relTool >= 0) {
                    ssb.setSpan(new ForegroundColorSpan(COLOR_WHITE), s + relTool, s + relTool + toolName.length(), Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                    ssb.setSpan(new StyleSpan(Typeface.BOLD), s + relTool, s + relTool + toolName.length(), Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);

                    int relArgs = relTool + toolName.length();
                    ssb.setSpan(new ForegroundColorSpan(COLOR_LIGHT_CYAN), s + relArgs, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);

                    int relHint = line.indexOf("(ctrl+o", relArgs);
                    if (relHint >= 0) {
                        ssb.setSpan(new ForegroundColorSpan(COLOR_MUTED), s + relHint, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                    }
                    return;
                }
            }
        }

        // 3. Response summary bullet line: ● The handoff is in ...
        if (trimmed.startsWith("●") || trimmed.startsWith("○")) {
            int relCircle = line.indexOf(trimmed.charAt(0));
            if (relCircle >= 0) {
                ssb.setSpan(new ForegroundColorSpan(COLOR_PURPLE), s + relCircle, s + relCircle + 1, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                ssb.setSpan(new StyleSpan(Typeface.BOLD), s + relCircle, s + relCircle + 1, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            }
            ssb.setSpan(new ForegroundColorSpan(COLOR_DEFAULT), s + relCircle + 1, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            highlightPathsAndMetrics(ssb, line, s, relCircle + 1, line.length());
            return;
        }

        // 4. Thinking block: ▸ Thought for ...
        Matcher thoughtM = THOUGHT_PATTERN.matcher(trimmed);
        if (thoughtM.matches()) {
            int relIcon = line.indexOf('▸');
            if (relIcon >= 0) {
                ssb.setSpan(new ForegroundColorSpan(COLOR_PURPLE), s + relIcon, s + relIcon + 1, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                ssb.setSpan(new StyleSpan(Typeface.BOLD), s + relIcon, s + relIcon + 1, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            }
            int relLabel = line.indexOf("Thought for");
            if (relLabel >= 0) {
                ssb.setSpan(new ForegroundColorSpan(COLOR_VIOLET), s + relLabel, s + relLabel + 11, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                ssb.setSpan(new ForegroundColorSpan(COLOR_YELLOW), s + relLabel + 11, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            }
            return;
        }

        // 5. Active Spinner: ⣻ Running command...
        Matcher spinnerM = SPINNER_PATTERN.matcher(trimmed);
        if (spinnerM.matches()) {
            int relSpin = line.indexOf(trimmed.charAt(0));
            if (relSpin >= 0) {
                ssb.setSpan(new ForegroundColorSpan(COLOR_AMBER), s + relSpin, s + relSpin + 1, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                ssb.setSpan(new ForegroundColorSpan(COLOR_YELLOW), s + relSpin + 1, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            }
            return;
        }

        // 6. Work Metric line: ✻ Worked for 59s · done 11:46 am
        if (trimmed.startsWith("✻") || trimmed.contains("Worked for")) {
            int relIcon = line.indexOf("✻");
            if (relIcon >= 0) {
                ssb.setSpan(new ForegroundColorSpan(COLOR_PURPLE), s + relIcon, s + relIcon + 1, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            }
            ssb.setSpan(new ForegroundColorSpan(COLOR_VIOLET), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            highlightPathsAndMetrics(ssb, line, s, 0, line.length());
            return;
        }

        // 7. Checkmarks: ✔ Update installed · Restart to update
        if (trimmed.contains("✔") || trimmed.contains("✓")) {
            int relCheck = Math.max(line.indexOf("✔"), line.indexOf("✓"));
            if (relCheck >= 0) {
                ssb.setSpan(new ForegroundColorSpan(COLOR_BRIGHT_GREEN), s + relCheck, s + relCheck + 1, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                ssb.setSpan(new StyleSpan(Typeface.BOLD), s + relCheck, s + relCheck + 1, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                ssb.setSpan(new ForegroundColorSpan(COLOR_BRIGHT_GREEN), s + relCheck + 1, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            }
            return;
        }

        // 8. Crossmarks / failures
        if (trimmed.contains("✖") || trimmed.contains("✗")) {
            ssb.setSpan(new ForegroundColorSpan(COLOR_RED), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            return;
        }

        // 9. Status footers: ⏵⏵ auto mode on ...
        if (trimmed.contains("⏵")) {
            ssb.setSpan(new ForegroundColorSpan(COLOR_CYAN), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            ssb.setSpan(new StyleSpan(Typeface.BOLD), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            return;
        }

        // 10. Footers: esc to cancel ... Gemini 3.8 Flash · high
        if (trimmed.contains("esc to cancel")) {
            int relEsc = line.indexOf("esc to cancel");
            if (relEsc >= 0) {
                ssb.setSpan(new ForegroundColorSpan(COLOR_MUTED), s, s + relEsc + 13, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            }
            int relModel = Math.max(line.indexOf("Gemini"), Math.max(line.indexOf("claude"), line.indexOf("gpt")));
            if (relModel >= 0) {
                ssb.setSpan(new ForegroundColorSpan(COLOR_GREEN), s + relModel, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                ssb.setSpan(new StyleSpan(Typeface.BOLD), s + relModel, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            }
            return;
        }

        // 11. Shell prompts: user@host:path$ cmd
        Matcher shellM = SHELL_PROMPT_PATTERN.matcher(trimmed);
        if (shellM.matches()) {
            String promptPart = shellM.group(1);
            int relPrompt = line.indexOf(promptPart);
            if (relPrompt >= 0) {
                ssb.setSpan(new ForegroundColorSpan(COLOR_GREEN), s + relPrompt, s + relPrompt + promptPart.length(), Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                ssb.setSpan(new StyleSpan(Typeface.BOLD), s + relPrompt, s + relPrompt + promptPart.length(), Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                ssb.setSpan(new ForegroundColorSpan(COLOR_WHITE), s + relPrompt + promptPart.length(), e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                ssb.setSpan(new StyleSpan(Typeface.BOLD), s + relPrompt + promptPart.length(), e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                return;
            }
        }

        // 12. Prompt symbols: > cmd or ❯ cmd or $ cmd
        Matcher promptM = PROMPT_PATTERN.matcher(trimmed);
        if (promptM.matches()) {
            int relSym = line.indexOf(trimmed.charAt(0));
            if (relSym >= 0) {
                ssb.setSpan(new ForegroundColorSpan(COLOR_CYAN), s + relSym, s + relSym + 1, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                ssb.setSpan(new StyleSpan(Typeface.BOLD), s + relSym, s + relSym + 1, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                ssb.setSpan(new ForegroundColorSpan(COLOR_WHITE), s + relSym + 1, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                ssb.setSpan(new StyleSpan(Typeface.BOLD), s + relSym + 1, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                return;
            }
        }

        // 13. Bullet lists with labels: - The project: ... or • Deliverables: ...
        Matcher listM = LIST_ITEM_PATTERN.matcher(line);
        if (listM.matches()) {
            String bullet = listM.group(1);
            String label = listM.group(2);
            int relBullet = line.indexOf(bullet);
            int relLabel = relBullet + bullet.length();
            int relRest = relLabel + label.length();

            // Bullet in Purple
            ssb.setSpan(new ForegroundColorSpan(COLOR_PURPLE), s + relBullet, s + relLabel, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            // Label in Vibrant Gold Bold
            ssb.setSpan(new ForegroundColorSpan(COLOR_YELLOW), s + relLabel, s + relRest, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            ssb.setSpan(new StyleSpan(Typeface.BOLD), s + relLabel, s + relRest, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            // Rest of text in Crisp Off-White
            ssb.setSpan(new ForegroundColorSpan(COLOR_DEFAULT), s + relRest, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            highlightPathsAndMetrics(ssb, line, s, relRest, line.length());
            return;
        }

        // 14. Numbered lists with labels: 1. Task Overview: ...
        Matcher numListM = NUMBERED_LIST_PATTERN.matcher(line);
        if (numListM.matches()) {
            String num = numListM.group(1);
            String label = numListM.group(2);
            int relNum = line.indexOf(num);
            int relLabel = relNum + num.length();
            int relRest = relLabel + label.length();

            // Number in Purple
            ssb.setSpan(new ForegroundColorSpan(COLOR_PURPLE), s + relNum, s + relLabel, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            // Label in Vibrant Gold Bold
            ssb.setSpan(new ForegroundColorSpan(COLOR_YELLOW), s + relLabel, s + relRest, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            ssb.setSpan(new StyleSpan(Typeface.BOLD), s + relLabel, s + relRest, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            // Rest in Crisp Off-White
            ssb.setSpan(new ForegroundColorSpan(COLOR_DEFAULT), s + relRest, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            highlightPathsAndMetrics(ssb, line, s, relRest, line.length());
            return;
        }

        // 15. Markdown Headers: ### 3. Build & Deployment
        if (MD_HEADING_PATTERN.matcher(trimmed).matches()) {
            ssb.setSpan(new ForegroundColorSpan(COLOR_VIOLET), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            ssb.setSpan(new StyleSpan(Typeface.BOLD), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            return;
        }

        // 16. Label Headers: User Requests:
        if (LABEL_HEADING_PATTERN.matcher(trimmed).matches()) {
            ssb.setSpan(new ForegroundColorSpan(COLOR_VIOLET), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            ssb.setSpan(new StyleSpan(Typeface.BOLD), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            return;
        }

        // 17. Errors & Exceptions
        String lower = trimmed.toLowerCase();
        if (lower.startsWith("error:") || lower.startsWith("fatal:") || lower.contains("exception") || lower.contains("traceback (most recent") || lower.startsWith("failed")) {
            ssb.setSpan(new ForegroundColorSpan(COLOR_RED), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            return;
        }

        // 18. Warnings
        if (lower.startsWith("warning:") || lower.startsWith("warn:")) {
            ssb.setSpan(new ForegroundColorSpan(COLOR_YELLOW), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            return;
        }

        // 19. Success
        if (lower.startsWith("success") || lower.contains("successfully") || lower.equals("ok") || lower.startsWith("passed")) {
            ssb.setSpan(new ForegroundColorSpan(COLOR_BRIGHT_GREEN), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            return;
        }

        // 20. Default regular text (idle / assistant response text)
        ssb.setSpan(new ForegroundColorSpan(COLOR_DEFAULT), s, e, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
        highlightPathsAndMetrics(ssb, line, s, 0, line.length());
    }

    private static void highlightPathsAndMetrics(SpannableStringBuilder ssb, String line, int lineStartInSsb, int minCol, int maxCol) {
        // Track ranges of paths to prevent number regex from overwriting path segments
        List<int[]> pathRanges = new ArrayList<>();

        // Highlight file paths, code tokens, and URLs in Electric Cyan
        Matcher pathM = PATH_OR_CODE_PATTERN.matcher(line);
        while (pathM.find()) {
            int colStart = pathM.start();
            int colEnd = pathM.end();
            if (colStart >= minCol && colEnd <= maxCol) {
                int pStart = lineStartInSsb + colStart;
                int pEnd = lineStartInSsb + colEnd;
                ssb.setSpan(new ForegroundColorSpan(COLOR_CYAN), pStart, pEnd, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                pathRanges.add(new int[]{colStart, colEnd});
            }
        }

        // Highlight durations, numbers, token metrics in Vibrant Amber/Gold
        Matcher numM = NUMBER_METRIC_PATTERN.matcher(line);
        while (numM.find()) {
            int colStart = numM.start();
            int colEnd = numM.end();
            if (colStart >= minCol && colEnd <= maxCol) {
                boolean overlaps = false;
                for (int[] range : pathRanges) {
                    if (colStart < range[1] && colEnd > range[0]) {
                        overlaps = true;
                        break;
                    }
                }
                if (!overlaps) {
                    int nStart = lineStartInSsb + colStart;
                    int nEnd = lineStartInSsb + colEnd;
                    ssb.setSpan(new ForegroundColorSpan(COLOR_YELLOW), nStart, nEnd, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                }
            }
        }

        // Highlight markdown bold **bold text** in Pure White Bold
        Matcher boldM = BOLD_PATTERN.matcher(line);
        while (boldM.find()) {
            int colStart = boldM.start(1);
            int colEnd = boldM.end(1);
            if (colStart >= minCol && colEnd <= maxCol) {
                int bStart = lineStartInSsb + colStart;
                int bEnd = lineStartInSsb + colEnd;
                ssb.setSpan(new ForegroundColorSpan(COLOR_WHITE), bStart, bEnd, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                ssb.setSpan(new StyleSpan(Typeface.BOLD), bStart, bEnd, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
            }
        }
    }
}
