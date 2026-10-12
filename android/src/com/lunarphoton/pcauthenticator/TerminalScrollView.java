package com.lunarphoton.pcauthenticator;

import android.content.Context;
import android.graphics.Rect;
import android.util.AttributeSet;
import android.view.MotionEvent;
import android.view.View;
import android.widget.ScrollView;

/**
 * Custom ScrollView for the terminal screen that suppresses unwanted
 * automatic scrolling to (0, 0) caused by TextView text updates, focus changes,
 * or selection resets, while still preserving touch gestures and native text selection.
 */
public class TerminalScrollView extends ScrollView {
    private boolean isUserTouching = false;

    public TerminalScrollView(Context context) {
        super(context);
    }

    public TerminalScrollView(Context context, AttributeSet attrs) {
        super(context, attrs);
    }

    public TerminalScrollView(Context context, AttributeSet attrs, int defStyleAttr) {
        super(context, attrs, defStyleAttr);
    }

    public boolean isUserTouching() {
        return isUserTouching;
    }

    @Override
    public boolean onInterceptTouchEvent(MotionEvent ev) {
        int action = ev.getActionMasked();
        if (action == MotionEvent.ACTION_DOWN) {
            isUserTouching = true;
        } else if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) {
            isUserTouching = false;
        }
        return super.onInterceptTouchEvent(ev);
    }

    @Override
    public boolean onTouchEvent(MotionEvent ev) {
        int action = ev.getActionMasked();
        if (action == MotionEvent.ACTION_DOWN) {
            isUserTouching = true;
        } else if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) {
            isUserTouching = false;
        }
        return super.onTouchEvent(ev);
    }

    @Override
    protected void onSizeChanged(int w, int h, int oldw, int oldh) {
        super.onSizeChanged(w, h, oldw, oldh);
        // The keyboard opening or closing changes the height. Keep the bottom edge on the same
        // line (that's where the prompt and the text being typed are); if the view was at the
        // end, stay at the end.
        if (oldh > 0 && h != oldh && getChildCount() > 0) {
            final int oldBottom = getScrollY() + oldh;      // scroll isn't clamped to the new size yet
            final boolean atEnd = getChildAt(0).getHeight() - oldBottom <= (int) (8 * getResources().getDisplayMetrics().density);
            post(() -> {
                int max = Math.max(0, getChildAt(0).getHeight() - getHeight());
                scrollTo(getScrollX(), atEnd ? max : Math.max(0, Math.min(max, oldBottom - getHeight())));
            });
        }
    }

    @Override
    public boolean requestChildRectangleOnScreen(View child, Rect rectangle, boolean immediate) {
        // Suppress automatic jump to (0,0) caused by TextView's internal setText() and selection resets
        return false;
    }

    @Override
    public void requestChildFocus(View child, View focused) {
        // Suppress ScrollView's automatic scrollToDescendant(focused) which forces jump to top when child is focused
    }
}
