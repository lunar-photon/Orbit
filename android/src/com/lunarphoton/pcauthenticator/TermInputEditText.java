package com.lunarphoton.pcauthenticator;

import android.content.Context;
import android.util.AttributeSet;
import android.view.KeyEvent;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;
import android.view.inputmethod.InputConnectionWrapper;
import android.widget.EditText;

/**
 * Terminal input box that reports backspace pressed while it's empty. Phone keyboards
 * mostly delete through the input connection (deleteSurroundingText) rather than sending
 * a key event, and some send nothing for an empty box unless asked, so both paths are watched.
 */
public class TermInputEditText extends EditText {

    public interface OnEmptyBackspace { void onEmptyBackspace(); }
    public interface OnEnter { void onEnter(); }

    private OnEmptyBackspace listener;
    private OnEnter enterListener;
    private long lastReport = 0;
    private long lastEnter = 0;

    public TermInputEditText(Context context, AttributeSet attrs) {
        super(context, attrs);
    }

    public void setOnEmptyBackspace(OnEmptyBackspace l) { listener = l; }

    /** The keyboard's Enter, however the keyboard reports it: an editor action of any kind,
     *  an Enter key event, or a typed newline. Some keyboards do the last two when the box is
     *  empty, which is exactly when Enter is needed to pick an item in a terminal menu. */
    public void setOnEnter(OnEnter l) { enterListener = l; }

    void reportEnter() {
        long now = System.currentTimeMillis();
        if (now - lastEnter < 150) return;      // one press can arrive through several paths
        lastEnter = now;
        if (enterListener != null) enterListener.onEnter();
    }

    private void report() {
        long now = System.currentTimeMillis();
        if (now - lastReport < 25) return;      // one press can arrive through both paths (held: ~50 ms apart)
        lastReport = now;
        if (listener != null) listener.onEmptyBackspace();
    }

    @Override
    public InputConnection onCreateInputConnection(EditorInfo outAttrs) {
        InputConnection base = super.onCreateInputConnection(outAttrs);
        if (base == null) return null;
        return new InputConnectionWrapper(base, true) {
            @Override
            public boolean deleteSurroundingText(int before, int after) {
                if (length() == 0 && before > 0) report();
                return super.deleteSurroundingText(before, after);
            }

            @Override
            public boolean deleteSurroundingTextInCodePoints(int before, int after) {
                if (length() == 0 && before > 0) report();
                return super.deleteSurroundingTextInCodePoints(before, after);
            }

            @Override
            public boolean sendKeyEvent(KeyEvent event) {
                if (event.getKeyCode() == KeyEvent.KEYCODE_DEL && event.getAction() == KeyEvent.ACTION_DOWN && length() == 0) {
                    report();
                }
                if ((event.getKeyCode() == KeyEvent.KEYCODE_ENTER || event.getKeyCode() == KeyEvent.KEYCODE_NUMPAD_ENTER)
                        && enterListener != null) {
                    if (event.getAction() == KeyEvent.ACTION_DOWN) reportEnter();
                    return true;
                }
                return super.sendKeyEvent(event);
            }

            @Override
            public boolean commitText(CharSequence text, int newCursorPosition) {
                if (enterListener != null && text != null && text.length() > 0
                        && (text.charAt(text.length() - 1) == '\n' || text.charAt(text.length() - 1) == '\r')) {
                    CharSequence before = text.subSequence(0, text.length() - 1);
                    boolean ok = before.length() == 0 || super.commitText(before, newCursorPosition);
                    reportEnter();
                    return ok;
                }
                return super.commitText(text, newCursorPosition);
            }

            @Override
            public boolean performEditorAction(int actionCode) {
                if (enterListener != null) {
                    reportEnter();
                    return true;
                }
                return super.performEditorAction(actionCode);
            }
        };
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        // hardware keyboards
        if ((keyCode == KeyEvent.KEYCODE_ENTER || keyCode == KeyEvent.KEYCODE_NUMPAD_ENTER) && enterListener != null) {
            reportEnter();
            return true;
        }
        if (keyCode == KeyEvent.KEYCODE_DEL && length() == 0) report();
        return super.onKeyDown(keyCode, event);
    }
}
