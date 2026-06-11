//! F6 — image assets: a caller-owned registry of DECODED pixel buffers.
//!
//! ## Architecture (docs/plan.md §F6)
//!
//! [`Assets`] mirrors the [`crate::Fonts`] shape: invariant I7 says core
//! never touches the filesystem and never decodes — callers (vyr-cli, tests,
//! an MCU boot path reading flash) hand in already-decoded RGBA8888 pixels
//! via [`Assets::register`], keyed by NAME. PNG decode lives in vyr-cli; on
//! MCU the F15 bake step plays the same role at build time. Core only ever
//! BLITS registered buffers ([`crate::Canvas::blit_image`]).
//!
//! ## The name contract (src resolution stays OUT of core)
//!
//! The registry key is the IR `src` attribute VERBATIM — an opaque string,
//! never a path core interprets. The shell resolves names to files (vyr-cli:
//! absolute path as-is, else `$VYR_ASSETS`, else CWD; LVGL's `A:` FS-driver
//! prefix is stripped there, defensively — the farm sends vyr the plain
//! path) and registers the decoded pixels under the SAME verbatim string the
//! IR carries, so the core lookup is exact-match and prefix-ignorant.
//!
//! ## Band equivalence (why blits are safe, same argument as glyphs)
//!
//! A registered image is decoded ONCE and blitted at INTEGER world positions
//! with deterministic integer source-over blending (`painter::blit_image`,
//! exact `round(x/255)` divisors). Per-pixel output depends only on world
//! position and existing dst — identical in every band by induction, the
//! glyph-blit argument verbatim. No scaling, no filtering, no float.
//!
//! ## Honest failure (invariant I6)
//!
//! A `src` that isn't registered is [`RenderError::MissingAsset`] naming the
//! assets that ARE available — an error BEFORE pixels, never a blank box.
//! Malformed registrations (byte count vs dimensions) are rejected at
//! registration time, not discovered mid-blit.

use alloc::collections::BTreeMap;
use alloc::format;
use alloc::string::{String, ToString};
use alloc::vec::Vec;

use crate::RenderError;

/// Largest accepted image side, in pixels. Keeps blit coordinates well inside
/// `i32` (world coords are |v| < 2^15 by the painter's quantization contract)
/// and rejects absurd decode results before they hit the per-pixel loop.
const MAX_SIDE: u32 = 16_384;

/// One decoded image: straight (non-premultiplied) RGBA8888, row-major,
/// `w * h * 4` bytes. Fields are private and the constructor validates, so an
/// inconsistent buffer is unrepresentable — the blit loop indexes by
/// construction, no per-pixel bounds defence. (Unlike [`crate::GlyphMask`],
/// which core itself produces, these bytes cross the caller boundary.)
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RgbaImage {
    w: u32,
    h: u32,
    rgba: Vec<u8>,
}

impl RgbaImage {
    /// Validate and wrap decoded pixels: nonzero dimensions, each side at
    /// most 16384, and exactly `w * h * 4` bytes of straight RGBA8888.
    pub fn new(w: u32, h: u32, rgba: Vec<u8>) -> Result<Self, RenderError> {
        if w == 0 || h == 0 || w > MAX_SIDE || h > MAX_SIDE {
            return Err(RenderError::BadAsset(format!(
                "image dimensions {w}x{h} out of range (1..={MAX_SIDE})"
            )));
        }
        let expect = (w as usize) * (h as usize) * 4; // ≤ 2^30, no overflow
        if rgba.len() != expect {
            return Err(RenderError::BadAsset(format!(
                "image {w}x{h} needs {expect} RGBA bytes, got {}",
                rgba.len()
            )));
        }
        Ok(Self { w, h, rgba })
    }

    pub fn w(&self) -> u32 {
        self.w
    }

    pub fn h(&self) -> u32 {
        self.h
    }

    /// Straight RGBA8888 pixels, row-major, `w * h * 4` bytes by construction.
    pub fn rgba(&self) -> &[u8] {
        &self.rgba
    }
}

/// The caller-owned image-asset registry (see module docs).
///
/// Long-lived by design, like [`crate::Fonts`]: register once, render many.
/// Read-only at render time (no lazy cache to fill — decode already
/// happened), so render paths take `&Assets`.
#[derive(Default)]
pub struct Assets {
    images: BTreeMap<String, RgbaImage>,
}

impl Assets {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register `image` under `name` — the VERBATIM IR `src` string (the
    /// name contract in the module docs). Duplicate names are rejected: two
    /// different buffers under one src would make renders ambiguous.
    pub fn register(&mut self, name: &str, image: RgbaImage) -> Result<(), RenderError> {
        if name.is_empty() {
            return Err(RenderError::BadAsset("empty asset name".into()));
        }
        if self.images.contains_key(name) {
            return Err(RenderError::BadAsset(format!(
                "asset {name:?} already registered"
            )));
        }
        self.images.insert(name.to_string(), image);
        Ok(())
    }

    /// Look up a registered asset by its verbatim `src` name. Missing =
    /// [`RenderError::MissingAsset`] naming what IS available (I6).
    pub fn get(&self, name: &str) -> Result<&RgbaImage, RenderError> {
        self.images.get(name).ok_or_else(|| {
            let available: Vec<&str> = self.images.keys().map(String::as_str).collect();
            RenderError::MissingAsset(format!(
                "asset {name:?} not registered (available: {available:?})"
            ))
        })
    }

    /// Registered asset names (sorted — BTreeMap order).
    pub fn names(&self) -> impl Iterator<Item = &str> {
        self.images.keys().map(String::as_str)
    }

    pub fn len(&self) -> usize {
        self.images.len()
    }

    pub fn is_empty(&self) -> bool {
        self.images.is_empty()
    }

    /// RGBA bytes currently held across all registered images (the RAM-budget
    /// number the F9 embedded spike reads, like `Fonts::cache_bytes`).
    pub fn bytes(&self) -> u64 {
        self.images.values().map(|i| i.rgba.len() as u64).sum()
    }
}
