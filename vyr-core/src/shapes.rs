//! [`Shapes`] — the caller-owned **flattened-contour memo** (#32).
//!
//! ## Why this exists
//!
//! The painter feeds tiny-skia ONLY polygons (see `painter` module docs): every
//! curve is flattened HERE, by vyr's own fixed-step rule, quantized to the
//! 1/64-px world grid. That flattening is `r·cos θ` / `r·sin θ` per vertex, and
//! on the M4F it is the most expensive thing in the frame — not because trig is
//! dear, but because `libm`'s f32 kernels evaluate in **f64** and a Cortex-M4F
//! FPU is single-precision, so every one of those multiplies is a
//! `__muldf3`/`__adddf3` compiler-builtin call. Measured on the 480x270 panel
//! fixture (`docs/measurements/lvgl-gap.md` §2.2): 4,976 `cosf` + 4,976 `sinf`
//! per frame driving 72,816 soft-double multiplies — **≈ 1,145 M4 instructions
//! per `cosf`**, ≈ 11.4 M insns/frame, 17.7 % of `Quality::Exact`.
//!
//! And it is *repeated* work. `circle_points`/`rrect_points` are pure functions
//! of their arguments, and a banded render re-runs them for **every band a
//! shape touches** (17 at the reference `BAND_H = 16`) and again every frame.
//!
//! ## Why it is invariant-safe
//!
//! This is a **pure memo and nothing else**: identical `f32` inputs, the same
//! `f32` outputs, bit for bit. No algebraic re-association (no "flatten at the
//! origin and translate" — `q(cx + r·cos θ)` is NOT `cx + q(r·cos θ)` in f32),
//! no radius bucketing, no rounding. Therefore the polygons are unchanged, the
//! 1/64-px quantization is unchanged, the exact-integer band translation is
//! unchanged, and every golden hash must stay put. That is the acceptance test
//! (`tests/shapes_cache.rs`), not a hope.
//!
//! ## Where it lives
//!
//! `vyr-core` is `no_std + alloc` and `forbid(unsafe_code)` (I7), so a hidden
//! global is not on the table and neither is interior mutability. The cache is
//! therefore **caller-owned and threaded in**, exactly like [`crate::Fonts`]
//! (glyph masks) and [`crate::Assets`] (decoded images) — hold one across the
//! band loop and across frames and the work happens once:
//!
//! ```ignore
//! let mut shapes = Shapes::new();
//! for band in bands {
//!     req.render_with_shapes(&mut fonts, &assets, &mut shapes, band, buf, stride, q)?;
//! }
//! ```
//!
//! The plain [`crate::render_with_quality`] entry points keep their shape and
//! use a throwaway cache, so nothing that does not care has to change.
//!
//! ## Why it is bounded
//!
//! The M4 heap arena is 122,880 B with a measured peak of 106,889 B at
//! `Quality::Fast` — a cache that outgrew that would be a regression, not a
//! win. [`Shapes`] is a **hard-budgeted** store ([`Shapes::DEFAULT_BUDGET`]):
//! once full it simply stops admitting entries and returns freshly flattened
//! contours, so the worst case is today's cost exactly. No eviction policy,
//! because eviction needs a recency clock and core has none (I7) — and a scene
//! whose distinct shapes do not fit is better served by a bigger budget the
//! caller chooses than by a cache that thrashes silently.
//!
//! What it actually costs, measured on the 480x270 panel fixture (plugin QEMU,
//! `release-mcu`, `scripts/tier-insns.py`):
//!
//! | tier | entries | cache B | M4 heap peak, before → after | insns/frame, before → after |
//! |---|--:|--:|--:|--:|
//! | `Exact` | 21 | 6,064 | 106,409 → 112,473 | 64,422,179 → 51,349,644 (−20.3 %) |
//! | `Fast` | 45 | 7,984 | 106,889 → 114,873 | 49,585,035 → 36,618,969 (−26.2 %) |
//! | `Draft` | 0 | 0 | 82,881 → 82,881 | 8,604,184 → 8,621,557 (+0.2 %) |
//!
//! The heap delta is the cache's own bytes to the byte — the memo returns an
//! owned contour, so the painter's allocation pattern is otherwise unchanged.
//! Draft holds nothing because it flattens nothing (its arcs are integer,
//! `painter::isqrt_i64`); its +0.2 % is `-Oz` code-layout drift, not cache
//! work. Worst case at `Fast` is 106,889 + 8,192 = 115,081 B against the
//! 122,880 B arena — 7,799 B still spare even if a scene fills the budget.

use alloc::vec::Vec;

/// A flattened contour: quantized world-space vertices, the only thing the
/// painter ever hands tiny-skia.
type Contour = Vec<(f32, f32)>;

/// Everything that determines a flattened contour — and **nothing that is
/// band-dependent**, which is the whole point: the same shape in band 3 and
/// band 11 must produce the same key or the memo never hits where the work is.
///
/// Floats are keyed by their **exact bit pattern** (`f32::to_bits`), never by
/// value comparison: a memo may only return a contour built from *identical*
/// inputs. (`-0.0` and `0.0` key apart and both build correctly — a duplicate
/// entry at worst, never a wrong one.)
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum Key {
    /// `painter::circle_points(cx, cy, r)`
    Circle { cx: i32, cy: i32, r: u32 },
    /// `painter::rrect_points(x, y, w, h, rad)`
    Rrect {
        x: u32,
        y: u32,
        w: u32,
        h: u32,
        rad: u32,
    },
    /// `painter::rrect_corner_points(x, y, w, h, rad, corner, cuts…)` — the
    /// Fast tier's per-corner cut contour. The four integer cut lines change
    /// the lead-in/lead-out vertices, so they are part of the identity.
    Corner {
        x: u32,
        y: u32,
        w: u32,
        h: u32,
        rad: u32,
        cuts: [i32; 4],
        corner: u8,
    },
}

/// One admitted contour: its identity plus its extent in the shared vertex
/// arena. Deliberately NOT a `Vec` per entry — 45 little `Vec`s (what
/// `Quality::Fast` holds for the reference fixture) is 45 allocator headers and
/// 45 chances to round a 40-byte contour up to a block, on a part with 122,880
/// B of heap in total.
struct Slot {
    key: Key,
    start: u32,
    len: u32,
}

/// Caller-owned cache of flattened contours (#32). Long-lived by design, like
/// [`crate::Fonts`]: build one, keep it across bands and frames, and each
/// distinct shape is flattened exactly once.
///
/// A pure memo — see the module docs for why that is what keeps band
/// equivalence byte-exact.
pub struct Shapes {
    /// Every admitted contour's vertices, back to back — ONE allocation.
    arena: Contour,
    /// Identity + extent of each contour in [`Self::arena`].
    index: Vec<Slot>,
    budget: usize,
    hits: u64,
    misses: u64,
    /// Flattenings that ran because the budget was full (the honesty number:
    /// a non-zero value means the budget, not the memo, is deciding).
    overflow: u64,
}

impl Shapes {
    /// Default cache budget, in heap bytes.
    ///
    /// Sized against the M4, which is the only place this matters: the arena is
    /// 122,880 B and `Quality::Fast` peaks at 106,889 B of it without a cache,
    /// so ~16 KB is all the headroom there is. 8 KiB spends half of that and
    /// leaves 7,799 B spare even in the worst case (a scene that fills the
    /// budget completely); the reference 480x270 panel fixture needs 7,984 B at
    /// `Fast` and 6,064 B at `Exact`, so it fits with the budget doing nothing.
    ///
    /// A scene that does NOT fit is not broken — [`Shapes::overflow`] counts
    /// the flattenings that had to run, so the loss is visible rather than
    /// mysterious, and [`Shapes::with_budget`] raises the ceiling on a part
    /// with more RAM. Raising it past ~15 KB on an F405-class part would put
    /// `Fast` into the wall, which is a worse bug than a slow frame.
    pub const DEFAULT_BUDGET: usize = 8 * 1024;

    /// An empty cache at [`Self::DEFAULT_BUDGET`]. Allocates nothing until the
    /// first contour is admitted, so `Quality::Draft` — which flattens no
    /// curves at all (its arcs are integer) — pays literally zero for holding
    /// one.
    pub fn new() -> Self {
        Self::with_budget(Self::DEFAULT_BUDGET)
    }

    /// An empty cache with an explicit heap budget in bytes. `0` disables
    /// caching entirely (every lookup flattens, i.e. the pre-#32 behaviour) —
    /// which is exactly what the equivalence test renders against.
    pub fn with_budget(budget: usize) -> Self {
        Self {
            arena: Vec::new(),
            index: Vec::new(),
            budget,
            hits: 0,
            misses: 0,
            overflow: 0,
        }
    }

    /// Heap bytes the cache is holding, **actual not nominal**: both stores are
    /// grown with `reserve_exact`, so capacity == length and this is the whole
    /// truth bar two allocator headers. That exactness is the point — a
    /// capacity-doubling `Vec` can hold twice the bytes it accounts for, and on
    /// a 122,880 B arena that is the difference between a budget and a hope.
    pub fn cache_bytes(&self) -> usize {
        self.arena.capacity() * core::mem::size_of::<(f32, f32)>()
            + self.index.capacity() * core::mem::size_of::<Slot>()
    }

    /// Distinct contours held.
    pub fn cache_entries(&self) -> usize {
        self.index.len()
    }

    /// Lookups served from the memo — the trig NOT executed.
    pub fn hits(&self) -> u64 {
        self.hits
    }

    /// Lookups that had to flatten because the shape was new.
    pub fn misses(&self) -> u64 {
        self.misses
    }

    /// Lookups that had to flatten because the budget was full. Non-zero means
    /// the scene has outgrown [`Self::DEFAULT_BUDGET`] and the caller should
    /// raise it ([`Self::with_budget`]) rather than wonder why the win shrank.
    pub fn overflow(&self) -> u64 {
        self.overflow
    }

    /// Drop every entry (the geometry is regenerable by construction; this is
    /// only ever a memory decision).
    pub fn clear(&mut self) {
        self.arena = Vec::new();
        self.index = Vec::new();
    }

    /// THE memo. Returns the flattened contour for `key`, running `build` only
    /// if it is not already held.
    ///
    /// Returns an **owned** contour rather than a borrow, deliberately: the
    /// painter's draw methods take `&mut self`, and handing back a borrow of a
    /// canvas field would fight the borrow checker at every call site (or need
    /// a checkout/restore dance with a leak on every early return). The copy is
    /// a `memcpy` of ~1 KB against the ~586,000 M4 instructions of soft-f64
    /// trig it replaces — 0.1 %, and it keeps the allocation profile *identical*
    /// to the pre-cache painter (one contour Vec per draw call), so the heap
    /// delta is the cache itself and nothing else.
    pub(crate) fn fetch(&mut self, key: Key, build: impl FnOnce() -> Contour) -> Contour {
        if let Some(slot) = self.index.iter().find(|s| s.key == key) {
            let (start, len) = (slot.start as usize, slot.len as usize);
            self.hits += 1;
            return self.arena[start..start + len].to_vec();
        }
        let pts = build();
        // Admit only if the WHOLE entry still fits: the vertices plus its slot.
        let cost = core::mem::size_of_val(&pts[..]) + core::mem::size_of::<Slot>();
        // `u32` extents: a contour is at most 256 vertices (the flattening step
        // rule caps per-quarter at 64) and the budget bounds the arena far
        // below 4 GiB, so this cannot truncate — but say so rather than assume.
        let fits = self.cache_bytes() + cost <= self.budget
            && u32::try_from(self.arena.len() + pts.len()).is_ok();
        if fits {
            self.misses += 1;
            let start = self.arena.len() as u32;
            // `reserve_exact`, never `push`-and-double: the budget must mean
            // bytes on the heap, not bytes we intended to use.
            self.arena.reserve_exact(pts.len());
            self.arena.extend_from_slice(&pts);
            self.index.reserve_exact(1);
            self.index.push(Slot {
                key,
                start,
                len: pts.len() as u32,
            });
        } else {
            self.overflow += 1;
        }
        pts
    }
}

impl Default for Shapes {
    fn default() -> Self {
        Self::new()
    }
}
