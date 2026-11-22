//! The app icon and the unmodified Powered by iBroadcast logos, drawn from the same SVG files
//! as the bundled browser page.
use eframe::egui::{self, TextureHandle, Vec2, vec2};
use resvg::{tiny_skia, usvg};

const ICON: &[u8] = include_bytes!("../../assets/ibfetch-icon.svg");
const POWERED_LIGHT: &[u8] = include_bytes!("../../assets/ibroadcast-light-compact-powered.svg");
const POWERED_DARK: &[u8] = include_bytes!("../../assets/ibroadcast-dark-compact-powered.svg");

pub struct Picture {
    texture: TextureHandle,
    pub size: Vec2,
}
impl Picture {
    /// Rasterize at the display's pixel density so the logo stays sharp.
    fn new(ctx: &egui::Context, name: &str, svg: &[u8], width: f32) -> Option<Self> {
        let tree = usvg::Tree::from_data(svg, &usvg::Options::default()).ok()?;
        let original = tree.size();
        let scale = width * ctx.pixels_per_point() / original.width();
        let pixels = [
            (original.width() * scale).ceil().max(1.) as u32,
            (original.height() * scale).ceil().max(1.) as u32,
        ];
        let mut pixmap = tiny_skia::Pixmap::new(pixels[0], pixels[1])?;
        resvg::render(
            &tree,
            tiny_skia::Transform::from_scale(scale, scale),
            &mut pixmap.as_mut(),
        );
        let image = egui::ColorImage::from_rgba_premultiplied(
            [pixels[0] as usize, pixels[1] as usize],
            pixmap.data(),
        );
        Some(Self {
            texture: ctx.load_texture(name, image, egui::TextureOptions::LINEAR),
            size: vec2(width, width * original.height() / original.width()),
        })
    }
    pub fn image(&self) -> egui::Image<'static> {
        egui::Image::from_texture((self.texture.id(), self.size))
    }
}
pub struct Art {
    density: f32,
    pub icon: Option<Picture>,
    powered_light: Option<Picture>,
    powered_dark: Option<Picture>,
}
impl Art {
    pub fn new(ctx: &egui::Context) -> Self {
        Self {
            density: ctx.pixels_per_point(),
            icon: Picture::new(ctx, "ibfetch-icon", ICON, 25.),
            powered_light: Picture::new(ctx, "powered-light", POWERED_LIGHT, 132.),
            powered_dark: Picture::new(ctx, "powered-dark", POWERED_DARK, 132.),
        }
    }
    /// Draw again after the window moves to a display with another pixel density.
    pub fn refresh(&mut self, ctx: &egui::Context) {
        if (self.density - ctx.pixels_per_point()).abs() > 0.01 {
            *self = Self::new(ctx);
        }
    }
    /// The official logo made for the current background.
    pub fn powered(&self, dark: bool) -> Option<&Picture> {
        if dark {
            self.powered_dark.as_ref()
        } else {
            self.powered_light.as_ref()
        }
    }
}
