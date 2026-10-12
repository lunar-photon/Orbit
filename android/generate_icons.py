#!/usr/bin/env python3
import os
from PIL import Image, ImageDraw, ImageFilter

def draw_shield_and_lock(canvas, size, center_y_ratio=0.5, scale_factor=1.0):
    """Draws the glowing security shield and lock emblem onto the given canvas."""
    cx = size / 2.0
    cy = size * center_y_ratio
    
    sw = size * 0.28 * scale_factor
    sh = size * 0.62 * scale_factor
    sy = cy - sh * 0.48
    
    shield_pts = [
        (cx, sy),
        (cx + sw, sy + sh * 0.2),
        (cx + sw * 0.9, sy + sh * 0.65),
        (cx, sy + sh),
        (cx - sw * 0.9, sy + sh * 0.65),
        (cx - sw, sy + sh * 0.2)
    ]
    
    # 1. Cyan outer glow
    glow = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(glow)
    g_draw.polygon(shield_pts, fill=(6, 182, 212, 130))
    glow = glow.filter(ImageFilter.GaussianBlur(max(2, int(size * 0.05 * scale_factor))))
    canvas.alpha_composite(glow)
    
    # 2. Outer Shield Body
    shield = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(shield)
    s_draw.polygon(shield_pts, fill=(15, 23, 42, 245), outline=(6, 182, 212, 255), width=max(1, int(size * 0.009 * scale_factor)))
    
    # Inset Shield border
    scale = 0.88
    inset_pts = [
        (cx, sy + (1 - scale) * sh),
        (cx + sw * scale, sy + sh * 0.2 * scale + (1 - scale) * sh * 0.5),
        (cx + sw * 0.9 * scale, sy + sh * 0.65 * scale + (1 - scale) * sh * 0.3),
        (cx, sy + sh * scale + (1 - scale) * sh * 0.1),
        (cx - sw * 0.9 * scale, sy + sh * 0.65 * scale + (1 - scale) * sh * 0.3),
        (cx - sw * scale, sy + sh * 0.2 * scale + (1 - scale) * sh * 0.5)
    ]
    s_draw.polygon(inset_pts, fill=(18, 32, 58, 240), outline=(16, 185, 129, 220), width=max(1, int(size * 0.006 * scale_factor)))
    canvas.alpha_composite(shield)
    
    # 3. Lock Graphic inside Shield (Zero white pixels - pure cyber cyan & emerald)
    fg = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    fg_draw = ImageDraw.Draw(fg)
    
    lock_w = int(size * 0.20 * scale_factor)
    lock_h = int(size * 0.16 * scale_factor)
    lock_y = int(cy - lock_h * 0.2)
    lock_x = int(cx - lock_w / 2.0)
    
    # Shackle (Arch) - High-tech glowing cyan (no white)
    shackle_r = int(lock_w * 0.36)
    shackle_thick = max(2, int(size * 0.024 * scale_factor))
    shackle_cx = int(cx)
    shackle_cy = lock_y - int(size * 0.03 * scale_factor)
    
    fg_draw.arc(
        [shackle_cx - shackle_r, shackle_cy - shackle_r, shackle_cx + shackle_r, shackle_cy + shackle_r],
        start=180, end=0, fill=(56, 189, 248, 255), width=shackle_thick
    )
    # Shackle legs
    leg_len = int(size * 0.05 * scale_factor)
    fg_draw.rectangle([shackle_cx - shackle_r, shackle_cy, shackle_cx - shackle_r + shackle_thick, shackle_cy + leg_len], fill=(56, 189, 248, 255))
    fg_draw.rectangle([shackle_cx + shackle_r - shackle_thick, shackle_cy, shackle_cx + shackle_r, shackle_cy + leg_len], fill=(56, 189, 248, 255))
    
    # Lock Body with emerald gradient look
    fg_draw.rounded_rectangle(
        [lock_x, lock_y, lock_x + lock_w, lock_y + lock_h],
        radius=max(2, int(size * 0.03 * scale_factor)),
        fill=(16, 185, 129, 255),
        outline=(52, 211, 153, 255),
        width=max(1, int(size * 0.01 * scale_factor))
    )
    
    # Keyhole
    kh_y = lock_y + int(lock_h * 0.32)
    kh_r = max(2, int(size * 0.020 * scale_factor))
    fg_draw.ellipse([cx - kh_r, kh_y - kh_r, cx + kh_r, kh_y + kh_r], fill=(11, 19, 41, 255))
    fg_draw.polygon([
        (cx - kh_r * 0.6, kh_y),
        (cx + kh_r * 0.6, kh_y),
        (cx + kh_r * 0.6, kh_y + kh_r * 1.8),
        (cx - kh_r * 0.6, kh_y + kh_r * 1.8)
    ], fill=(11, 19, 41, 255))
    
    # Laptop base pedestal
    base_y = lock_y + lock_h + int(size * 0.038 * scale_factor)
    base_w = int(size * 0.30 * scale_factor)
    base_h = max(2, int(size * 0.020 * scale_factor))
    fg_draw.rounded_rectangle([cx - base_w / 2.0, base_y, cx + base_w / 2.0, base_y + base_h], radius=max(1, int(size * 0.008 * scale_factor)), fill=(56, 189, 248, 240))
    notch_w = int(size * 0.06 * scale_factor)
    fg_draw.rectangle([cx - notch_w / 2.0, base_y, cx + notch_w / 2.0, base_y + int(base_h * 0.45)], fill=(11, 19, 41, 255))
    
    canvas.alpha_composite(fg)

def create_base_icon(size=512, is_round=False):
    """Creates a full-bleed legacy icon with NO white padding or transparent margins."""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    
    # Full bleed background (edge-to-edge dark cyber navy gradient)
    bg = Image.new('RGBA', (size, size), (11, 19, 41, 255))
    bg_draw = ImageDraw.Draw(bg)
    
    cx, cy = size / 2.0, size / 2.0
    max_r = int(size * 0.72)
    for r in range(max_r, 0, -2):
        factor = r / float(max_r)
        # Deep obsidian to rich cyber teal-navy
        r_c = int(11 + 20 * factor)
        g_c = int(19 + 28 * factor)
        b_c = int(41 + 45 * factor)
        bg_draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(r_c, g_c, b_c, 255))
    
    if is_round:
        mask = Image.new('L', (size, size), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.ellipse([0, 0, size, size], fill=255)
        img.paste(bg, (0, 0), mask)
        
        # Subtle border ring
        b_draw = ImageDraw.Draw(img)
        b_draw.ellipse([1, 1, size - 1, size - 1], outline=(56, 189, 248, 140), width=max(1, int(size * 0.012)))
    else:
        # Full-bleed square - completely fills the canvas edge-to-edge!
        img.paste(bg, (0, 0))
        b_draw = ImageDraw.Draw(img)
        b_draw.rectangle([0, 0, size - 1, size - 1], outline=(56, 189, 248, 100), width=max(1, int(size * 0.008)))
    
    # Draw emblem
    draw_shield_and_lock(img, size, center_y_ratio=0.5, scale_factor=0.95)
    return img

def create_adaptive_foreground(size=432):
    """Creates an adaptive icon foreground layer within the 72dp safe zone."""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    # Safe zone scale: ~62% of full 108dp canvas
    draw_shield_and_lock(img, size, center_y_ratio=0.5, scale_factor=0.62)
    return img

def main():
    base_dir = "/home/lunarphoton/.local/share/pc-authenticator-android/res"
    
    # Legacy icon densities (48dp base)
    legacy_sizes = {
        "mipmap-mdpi": 48,
        "mipmap-hdpi": 72,
        "mipmap-xhdpi": 96,
        "mipmap-xxhdpi": 144,
        "mipmap-xxxhdpi": 192,
    }
    
    # Adaptive foreground densities (108dp base)
    adaptive_sizes = {
        "mipmap-mdpi": 108,
        "mipmap-hdpi": 162,
        "mipmap-xhdpi": 216,
        "mipmap-xxhdpi": 324,
        "mipmap-xxxhdpi": 432,
    }
    
    icon_512 = create_base_icon(512, is_round=False)
    round_512 = create_base_icon(512, is_round=True)
    fg_432 = create_adaptive_foreground(432)
    
    # Save 512x512 drawables
    os.makedirs(os.path.join(base_dir, "drawable"), exist_ok=True)
    icon_512.save(os.path.join(base_dir, "drawable", "ic_launcher.png"), "PNG")
    round_512.save(os.path.join(base_dir, "drawable", "ic_launcher_round.png"), "PNG")
    fg_432.save(os.path.join(base_dir, "drawable", "ic_launcher_foreground.png"), "PNG")
    
    # Save mipmaps
    for folder, dim in legacy_sizes.items():
        folder_path = os.path.join(base_dir, folder)
        os.makedirs(folder_path, exist_ok=True)
        
        # Legacy square & round icons (full bleed, no whitespace)
        sq = icon_512.resize((dim, dim), Image.Resampling.LANCZOS)
        sq.save(os.path.join(folder_path, "ic_launcher.png"), "PNG")
        
        rd = round_512.resize((dim, dim), Image.Resampling.LANCZOS)
        rd.save(os.path.join(folder_path, "ic_launcher_round.png"), "PNG")
        
        # Adaptive foreground layer (108dp grid)
        fg_dim = adaptive_sizes[folder]
        fg = fg_432.resize((fg_dim, fg_dim), Image.Resampling.LANCZOS)
        fg.save(os.path.join(folder_path, "ic_launcher_foreground.png"), "PNG")
        
    # Also update web server static icons
    static_dir = "/home/lunarphoton/.config/pc-authenticator/static"
    if os.path.exists(static_dir):
        icon_512.resize((192, 192), Image.Resampling.LANCZOS).save(os.path.join(static_dir, "icon-192.png"), "PNG")
        icon_512.save(os.path.join(static_dir, "icon-512.png"), "PNG")
        
    print("✅ All full-bleed & adaptive icons generated successfully with 0 whitespace!")

if __name__ == '__main__':
    main()
