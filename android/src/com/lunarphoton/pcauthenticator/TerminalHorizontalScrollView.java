package com.lunarphoton.pcauthenticator;

import android.content.Context;
import android.graphics.Rect;
import android.util.AttributeSet;
import android.view.View;
import android.widget.HorizontalScrollView;

/**
 * Custom HorizontalScrollView for the terminal screen that suppresses unwanted
 * automatic scrolling to (0, 0) caused by child focus changes or selection resets.
 */
public class TerminalHorizontalScrollView extends HorizontalScrollView {
    public TerminalHorizontalScrollView(Context context) {
        super(context);
    }

    public TerminalHorizontalScrollView(Context context, AttributeSet attrs) {
        super(context, attrs);
    }

    public TerminalHorizontalScrollView(Context context, AttributeSet attrs, int defStyleAttr) {
        super(context, attrs, defStyleAttr);
    }

    @Override
    public boolean requestChildRectangleOnScreen(View child, Rect rectangle, boolean immediate) {
        // Suppress automatic jump to (0,0) caused by child focus or selection resets
        return false;
    }

    @Override
    public void requestChildFocus(View child, View focused) {
        // Suppress HorizontalScrollView's automatic scrollToDescendant(focused)
    }
}
