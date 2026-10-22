//! Follows the live Omarchy theme and font.
//!
//! Colours come from `~/.local/state/omarchy/current/theme/colors.toml`. A
//! theme switch builds the new theme beside the old one and swaps it into
//! place, rewriting `theme.generation`, so the app checks a cheap fingerprint
//! of those files once a second instead of holding inotify watches that a
//! directory swap would orphan. The font is fontconfig's monospace family,
//! which is what `omarchy-font-set` changes and `omarchy-font-current` reads.

use iced::{Color, Font, font};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::SystemTime;

#[derive(Debug, Clone, PartialEq)]
pub struct Colors {
    pub accent: Color,
    pub background: Color,
    pub dark_background: Color,
    pub lighter_background: Color,
    pub foreground: Color,
    pub muted: Color,
    pub red: Color,
    pub yellow: Color,
    pub green: Color,
    pub blue: Color,
    pub magenta: Color,
    pub cyan: Color,
}

#[derive(Debug, Clone)]
pub struct OmarchyTheme {
    pub colors: Colors,
    pub light: bool,
    pub font: Font,
    pub family: String,
    home: PathBuf,
    fingerprint: Fingerprint,
}

#[derive(Debug, Clone, PartialEq, Default)]
struct Fingerprint {
    files: Vec<Option<(SystemTime, u64)>>,
}

fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

fn current_dir(home: &Path) -> PathBuf {
    home.join(".local/state/omarchy/current")
}

fn watched_files(home: &Path) -> Vec<PathBuf> {
    let current = current_dir(home);
    vec![
        current.join("theme.generation"),
        current.join("theme.name"),
        current.join("theme/colors.toml"),
        // Where omarchy-font-set records the monospace family.
        home.join(".config/fontconfig/fonts.conf"),
    ]
}

fn fingerprint(home: &Path) -> Fingerprint {
    Fingerprint {
        files: watched_files(home)
            .iter()
            .map(|p| std::fs::metadata(p).ok().and_then(|m| Some((m.modified().ok()?, m.len()))))
            .collect(),
    }
}

/// A dark palette in Omarchy's default shape, used for any colour a theme
/// leaves out and when Omarchy is not installed.
fn defaults() -> Colors {
    Colors {
        accent: hex("#FFD60A").unwrap(),
        background: hex("#1a1b26").unwrap(),
        dark_background: hex("#16161e").unwrap(),
        lighter_background: hex("#24283b").unwrap(),
        foreground: hex("#c0caf5").unwrap(),
        muted: hex("#565f89").unwrap(),
        red: hex("#f7768e").unwrap(),
        yellow: hex("#e0af68").unwrap(),
        green: hex("#9ece6a").unwrap(),
        blue: hex("#7aa2f7").unwrap(),
        magenta: hex("#bb9af7").unwrap(),
        cyan: hex("#7dcfff").unwrap(),
    }
}

pub fn hex(s: &str) -> Option<Color> {
    let s = s.trim().trim_start_matches('#');
    if s.len() != 6 {
        return None;
    }
    let v = u32::from_str_radix(s, 16).ok()?;
    Some(Color::from_rgb8((v >> 16) as u8, (v >> 8) as u8, v as u8))
}

/// Reads every `name = "#rrggbb"` line over the defaults, plus `mode`.
pub fn read_colors(path: &Path) -> (Colors, bool) {
    let mut c = defaults();
    let mut light = false;
    let Ok(text) = std::fs::read_to_string(path) else {
        return (c, light);
    };
    let mut dark_background = None;
    for line in text.lines() {
        let Some((key, value)) = line.split_once('=') else { continue };
        let key = key.trim();
        let value = value.trim().trim_matches(|ch| ch == '"' || ch == '\'');
        if key == "mode" {
            light = value == "light";
            continue;
        }
        let Some(color) = hex(value) else { continue };
        match key {
            "accent" => c.accent = color,
            "background" => c.background = color,
            "dark_background" => dark_background = Some(color),
            "lighter_background" => c.lighter_background = color,
            "foreground" => c.foreground = color,
            "muted" => c.muted = color,
            "red" => c.red = color,
            "yellow" => c.yellow = color,
            "green" => c.green = color,
            "blue" => c.blue = color,
            "magenta" => c.magenta = color,
            "cyan" => c.cyan = color,
            _ => {}
        }
    }
    // Older themes have no dark_background; derive one from the background.
    c.dark_background = dark_background.unwrap_or_else(|| mix(c.background, c.foreground, 0.04));
    if !text.contains("mode") {
        light = luminance(c.background) > 0.5;
    }
    (c, light)
}

fn current_family() -> String {
    Command::new("fc-match")
        .args(["monospace", "-f", "%{family}"])
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .and_then(|s| s.split(',').next().map(|f| f.trim().to_string()))
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "monospace".to_string())
}

impl OmarchyTheme {
    pub fn load() -> Self {
        Self::load_from(home())
    }

    /// Loads the theme of the Omarchy install under `home` (tests pass a
    /// temporary directory).
    pub fn load_from(home: PathBuf) -> Self {
        let (colors, light) = read_colors(&current_dir(&home).join("theme/colors.toml"));
        let family = current_family();
        Self {
            colors,
            light,
            font: font_for(&family),
            family,
            fingerprint: fingerprint(&home),
            home,
        }
    }

    /// Reloads when the theme or font files changed; returns whether they did.
    pub fn refresh(&mut self) -> bool {
        let now = fingerprint(&self.home);
        if now == self.fingerprint {
            return false;
        }
        let (colors, light) = read_colors(&current_dir(&self.home).join("theme/colors.toml"));
        let family = current_family();
        if family != self.family {
            self.font = font_for(&family);
            self.family = family;
        }
        self.colors = colors;
        self.light = light;
        self.fingerprint = now;
        true
    }

    pub fn bold(&self) -> Font {
        Font { weight: font::Weight::Bold, ..self.font }
    }

    pub fn iced_theme(&self) -> iced::Theme {
        let c = &self.colors;
        iced::Theme::custom(
            "Omarchy",
            iced::theme::Palette {
                background: c.background,
                text: c.foreground,
                primary: c.accent,
                success: c.green,
                warning: c.yellow,
                danger: c.red,
            },
        )
    }

    /// Background for the selected row: a tint of the accent, which reads on
    /// light and dark themes alike (light themes' selection greys often don't).
    pub fn selection(&self) -> Color {
        mix(self.colors.background, self.colors.accent, if self.light { 0.22 } else { 0.28 })
    }

    /// A hairline colour for borders between panes.
    pub fn line(&self) -> Color {
        mix(self.colors.background, self.colors.foreground, 0.14)
    }

    /// Black or white, whichever reads on the accent (WCAG luminance).
    pub fn on_accent(&self) -> Color {
        if luminance(self.colors.accent) > 0.179 { Color::BLACK } else { Color::WHITE }
    }
}

/// iced names fonts by `&'static str`. Font changes are rare, so leaking the
/// family name on each change costs a few bytes at most.
fn font_for(family: &str) -> Font {
    Font::with_name(Box::leak(family.to_string().into_boxed_str()))
}

pub fn mix(a: Color, b: Color, t: f32) -> Color {
    Color {
        r: a.r + (b.r - a.r) * t,
        g: a.g + (b.g - a.g) * t,
        b: a.b + (b.b - a.b) * t,
        a: 1.0,
    }
}

fn luminance(c: Color) -> f32 {
    let lin = |v: f32| if v <= 0.04045 { v / 12.92 } else { ((v + 0.055) / 1.055).powf(2.4) };
    0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_colors_and_mode() {
        let dir = std::env::temp_dir().join(format!("verbatim-theme-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("colors.toml");
        std::fs::write(&path, "mode = \"light\"\naccent = \"#00b4c8\"\nbackground = '#fafcff'\n").unwrap();
        let (c, light) = read_colors(&path);
        assert!(light);
        assert_eq!(c.accent, hex("#00b4c8").unwrap());
        assert_eq!(c.background, hex("#fafcff").unwrap());
        // Missing colours fall back to the defaults.
        assert_eq!(c.red, defaults().red);
        std::fs::remove_dir_all(dir).ok();
    }

    #[test]
    fn follows_a_theme_switch() {
        let home = std::env::temp_dir().join(format!("verbatim-home-{}", std::process::id()));
        let state = home.join(".local/state/omarchy");
        std::fs::create_dir_all(state.join("current/theme")).unwrap();
        std::fs::write(state.join("current/theme/colors.toml"), "accent = \"#7daea3\"\nbackground = \"#282828\"\n").unwrap();
        std::fs::write(state.join("current/theme.generation"), "1").unwrap();
        let mut theme = OmarchyTheme::load_from(home.clone());
        assert_eq!(theme.colors.accent, hex("#7daea3").unwrap());
        assert!(!theme.light);
        assert!(!theme.refresh(), "nothing changed yet");

        // Omarchy builds the next theme beside the current one and swaps it in.
        std::fs::create_dir_all(state.join("next")).unwrap();
        std::fs::write(state.join("next/colors.toml"), "mode = \"light\"\naccent = \"#56949f\"\nbackground = \"#faf4ed\"\n").unwrap();
        std::fs::rename(state.join("current/theme"), state.join("old")).unwrap();
        std::fs::rename(state.join("next"), state.join("current/theme")).unwrap();
        std::fs::write(state.join("current/theme.generation"), "2").unwrap();
        assert!(theme.refresh());
        assert_eq!(theme.colors.accent, hex("#56949f").unwrap());
        assert!(theme.light);
        std::fs::remove_dir_all(home).ok();
    }

    #[test]
    fn missing_file_gives_defaults() {
        let (c, light) = read_colors(Path::new("/nonexistent/colors.toml"));
        assert_eq!(c, defaults());
        assert!(!light);
    }
}
