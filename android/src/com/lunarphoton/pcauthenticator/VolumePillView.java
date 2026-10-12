package com.lunarphoton.pcauthenticator;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.graphics.drawable.Drawable;
import android.util.AttributeSet;
import android.view.HapticFeedbackConstants;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;

/** Tall rounded volume control: fills from the bottom, drag anywhere on it to set the level. */
public class VolumePillView extends View {

    public interface Listener {
        void onChanging(int percent);   // while the finger moves
        void onChanged(int percent);    // finger lifted
    }

    private final Paint track = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint label = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final RectF bounds = new RectF();
    private final Path clip = new Path();
    private final Drawable iconLow, iconHigh;
    private final float density;
    private int percent = -1;           // -1 = unknown until the computer reports it
    private boolean dragging;
    private Listener listener;

    public VolumePillView(Context context, AttributeSet attrs) {
        super(context, attrs);
        density = getResources().getDisplayMetrics().density;
        track.setColor(context.getColor(R.color.o_stroke));
        fill.setColor(context.getColor(R.color.o_pink));
        label.setTextAlign(Paint.Align.CENTER);
        label.setTextSize(12 * getResources().getDisplayMetrics().scaledDensity);
        label.setTypeface(Typeface.MONOSPACE);
        if (android.os.Build.VERSION.SDK_INT >= 26) {
            try { label.setTypeface(getResources().getFont(R.font.geist_mono_medium)); } catch (Exception ignored) { }
        }
        iconLow = context.getDrawable(R.drawable.ic_o_volume_down).mutate();
        iconHigh = context.getDrawable(R.drawable.ic_o_volume_up).mutate();
        setFocusable(true);
        setClickable(true);
        setContentDescription("Volume");
    }

    public void setListener(Listener l) { listener = l; }

    public boolean isDragging() { return dragging; }

    /** Show the level the computer reports (ignored while the user is dragging). */
    public void setPercent(int p) {
        if (dragging) return;
        p = Math.max(-1, Math.min(100, p));
        if (p != percent) {
            percent = p;
            describe(p < 0 ? "Unknown" : p + "%");
            invalidate();
        }
    }

    private void describe(String state) {
        if (android.os.Build.VERSION.SDK_INT >= 30) setStateDescription(state);
        else setContentDescription("Volume " + state);
    }

    @Override
    protected void onDraw(Canvas canvas) {
        float r = Math.min(getWidth(), getHeight()) / 2f;
        bounds.set(0, 0, getWidth(), getHeight());
        clip.reset();
        clip.addRoundRect(bounds, r, r, Path.Direction.CW);

        canvas.save();
        canvas.clipPath(clip);
        canvas.drawRect(bounds, track);
        float level = Math.max(percent, 0) / 100f;
        float top = getHeight() * (1 - level);
        canvas.drawRect(0, top, getWidth(), getHeight(), fill);
        canvas.restore();

        int onFill = getContext().getColor(R.color.o_on_accent);
        int onTrack = getContext().getColor(R.color.o_text);
        int onTrackDim = getContext().getColor(R.color.o_text_2);

        // icon near the bottom, dark when the fill covers it
        int size = Math.round(20 * density);
        int cx = getWidth() / 2;
        int iconBottom = getHeight() - Math.round(12 * density);
        Drawable icon = percent >= 50 ? iconHigh : iconLow;
        icon.setBounds(cx - size / 2, iconBottom - size, cx + size / 2, iconBottom);
        icon.setTint(top <= iconBottom - size / 2f ? onFill : onTrackDim);
        icon.draw(canvas);

        // number near the top, readable on either colour
        float baseline = 22 * density;
        label.setColor(top <= baseline - label.getTextSize() / 2 ? onFill : onTrack);
        canvas.drawText(percent < 0 ? "--" : String.valueOf(percent), cx, baseline, label);
    }

    private int percentAt(float y) {
        float pad = 6 * density;        // so 0 and 100 are easy to hit at the ends
        float usable = getHeight() - 2 * pad;
        float v = 1 - (y - pad) / usable;
        return Math.round(Math.max(0, Math.min(1, v)) * 100);
    }

    private void update(int p, boolean finished) {
        boolean changed = p != percent;
        percent = p;
        if (changed) {
            if (p == 0 || p == 100) performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK);
            describe(p + "%");
            invalidate();
        }
        if (listener != null) {
            if (finished) listener.onChanged(p);
            else if (changed) listener.onChanging(p);
        }
    }

    @Override
    public boolean onTouchEvent(MotionEvent e) {
        switch (e.getActionMasked()) {
            case MotionEvent.ACTION_DOWN:
                dragging = true;
                getParent().requestDisallowInterceptTouchEvent(true);   // keep the page from scrolling
                update(percentAt(e.getY()), false);
                return true;
            case MotionEvent.ACTION_MOVE:
                update(percentAt(e.getY()), false);
                return true;
            case MotionEvent.ACTION_UP:
            case MotionEvent.ACTION_CANCEL:
                dragging = false;
                getParent().requestDisallowInterceptTouchEvent(false);
                update(percentAt(e.getY()), true);
                return true;
        }
        return super.onTouchEvent(e);
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        int step = keyCode == KeyEvent.KEYCODE_DPAD_UP ? 5 : keyCode == KeyEvent.KEYCODE_DPAD_DOWN ? -5 : 0;
        if (step == 0) return super.onKeyDown(keyCode, event);
        update(Math.max(0, Math.min(100, Math.max(percent, 0) + step)), true);
        return true;
    }
}
