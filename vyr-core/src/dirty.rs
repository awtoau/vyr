//! F3 — dirty-rect tracking: the retained-mode repaint primitive.
//!
//! "If I move this widget, how does the system know what to redraw?" —
//! [`dirty_rects`] answers it: diff two IR requests and return the screen
//! regions whose pixels may differ. A changed / added / removed subtree
//! contributes its OLD paint bbox (**WAS** — whatever it covered must be
//! repainted underneath) and its NEW paint bbox (**NOW**). The caller then
//! repaints ONLY those regions through THE banding entry point
//! (`render(tree, area, buf, stride)`, invariant I1) — a tall dirty region
//! is just a few bands through the working buffer; tree order is z-order, so
//! overlapping widgets restack correctly by construction.
//!
//! ## Why this is byte-exact (the band-equivalence dividend)
//!
//! Correctness needs exactly one property: the dirty set is a SUPERSET of
//! the pixels that differ between the two full frames. Inside a dirty rect,
//! `render(area = rect)` reproduces the full frame's bytes — that is the
//! band-equivalence invariant the painter is built around (and dirty rects
//! are its first ARBITRARY-rectangle consumer: x-offsets and vertical seams,
//! not just full-width horizontal bands). Outside, prev's pixels are already
//! next's pixels, by the superset property. So incremental == full render,
//! byte-identical — `tests/dirty_rects.rs` proves it for moves, restyles,
//! removals and no-ops.
//!
//! ## Conservatism (v1, deliberate)
//!
//! - **Subtree granularity**: nodes pair by index; a changed node dirties
//!   its whole subtree's bbox (children included). Repainting too much is
//!   always safe — never wrong, only slower; the merge step coalesces
//!   overlap.
//! - **Paint bboxes are bounds, not measurements**: a node's box is its rect
//!   ∪ a text-run bound (chars × an em-width bound — text can ink past its
//!   declared rect; centred runs bound around the PARENT centre, the
//!   cases.py contract), inflated by border-half + AA margin, then
//!   intersected with the nearest clipping ancestor's rect (the F3 clip
//!   stack means paint genuinely cannot escape it — [`crate::ir`]'s
//!   `clips_children` is shared so the diff and the walk can never disagree
//!   about who clips).
//! - **Attr equality is structural**: any attr change dirties the node, even
//!   ones vyr doesn't paint (unknown attrs). Honest over clever.
//! - Root attr / screen-size changes dirty the WHOLE screen (the backdrop is
//!   behind everything).
//!
//! [`crate::ir::Request::render_incremental`] packages the loop (diff →
//! repaint rects onto prev's frame) and wires
//! [`crate::RenderStats::dirty_area_px`].

use alloc::vec::Vec;

use crate::ir::{Node, Request, clips_children};
use crate::{Assets, Fonts, Rect, RenderError, RenderStats};

/// Paint can spill past pure geometry by ~1 px of AA plus quantization;
/// 2 px absorbs it (the painter's own op-margin reasoning).
const AA_MARGIN: u32 = 2;
/// The vyvanse IR default font size (FontSpec size=14) — the text-bound
/// fallback when a node names no size.
const DEFAULT_FONT_SIZE: u32 = 14;

/// The regions of `next`'s screen whose pixels may differ from a full render
/// of `prev` — merged, clamped to the screen, empty when the trees are
/// identical. See module docs for the WAS/NOW model and the conservatism
/// rules.
pub fn dirty_rects(prev: &Request, next: &Request) -> Vec<Rect> {
    let screen = Rect {
        x: 0,
        y: 0,
        w: next.w,
        h: next.h,
    };
    // Screen geometry or root (backdrop) change: everything repaints.
    if prev.w != next.w
        || prev.h != next.h
        || prev.root.name != next.root.name
        || prev.root.attrs != next.root.attrs
    {
        return alloc::vec![screen];
    }
    let mut out = Vec::new();
    // The screen root never clips via push (the band is the outermost clip)
    // but nothing paints outside it either — it IS the clip context here.
    diff_children(
        &prev.root.children,
        &next.root.children,
        screen,
        screen,
        screen,
        &mut out,
    );
    let mut rects: Vec<Rect> = out
        .into_iter()
        .map(|r| r.intersect(screen))
        .filter(|r| !r.is_empty())
        .collect();
    merge(&mut rects);
    rects
}

/// Pairwise (by index — z-order) diff of two child lists. `p_clip` /
/// `n_clip` are the prev/next clip contexts: they only ever differ when one
/// side has no children at all (so no common pairs exist).
fn diff_children(
    p: &[Node],
    n: &[Node],
    parent: Rect,
    p_clip: Rect,
    n_clip: Rect,
    out: &mut Vec<Rect>,
) {
    let common = p.len().min(n.len());
    for i in 0..common {
        diff_nodes(&p[i], &n[i], parent, p_clip, out);
    }
    for removed in &p[common..] {
        push_subtree(removed, parent, p_clip, out); // WAS
    }
    for added in &n[common..] {
        push_subtree(added, parent, n_clip, out); // NOW
    }
}

fn diff_nodes(p: &Node, n: &Node, parent: Rect, clip: Rect, out: &mut Vec<Rect>) {
    if p.name != n.name || p.attrs != n.attrs {
        // Changed node: WAS + NOW, whole subtree (module docs).
        push_subtree(p, parent, clip, out);
        push_subtree(n, parent, clip, out);
        return;
    }
    // Identical name+attrs ⇒ identical own paint at identical absolute
    // geometry (parents equal by induction) — recurse into children.
    let r = node_rect(n, parent);
    let p_clip = if clips_children(p) {
        clip.intersect(r)
    } else {
        clip
    };
    let n_clip = if clips_children(n) {
        clip.intersect(r)
    } else {
        clip
    };
    diff_children(&p.children, &n.children, r, p_clip, n_clip, out);
}

/// Contribute one whole subtree's paint bbox(es), clip-context aware.
fn push_subtree(n: &Node, parent: Rect, clip: Rect, out: &mut Vec<Rect>) {
    if clip.is_empty() {
        return; // nothing inside an empty clip region can paint
    }
    let r = node_rect(n, parent);
    let own = paint_bbox(n, r, parent).intersect(clip);
    if !own.is_empty() {
        out.push(own);
    }
    let child_clip = if clips_children(n) {
        clip.intersect(r)
    } else {
        clip
    };
    for child in &n.children {
        push_subtree(child, r, child_clip, out);
    }
}

/// A node's absolute rect — MUST mirror the walk's geometry resolution
/// (`parent + x/y`, `width`/`height`, zero defaults).
fn node_rect(n: &Node, parent: Rect) -> Rect {
    Rect {
        x: parent.x + n.i32_attr("x", 0),
        y: parent.y + n.i32_attr("y", 0),
        w: n.u32_attr("width", 0),
        h: n.u32_attr("height", 0),
    }
}

/// Upper bound on the font size a node's text could render at: every size
/// the walk's font selection could pick (explicit `font_size`, a
/// `style_text_font` `_<size>` suffix, the default) — max of all candidates.
fn font_size_bound(n: &Node) -> u32 {
    let mut size = DEFAULT_FONT_SIZE;
    size = size.max(n.u32_attr("font_size", 0));
    if let Some(ltf) = n.str_attr("style_text_font")
        && let Some((_, s)) = ltf.rsplit_once('_')
        && let Ok(s) = s.parse::<u32>()
    {
        size = size.max(s);
    }
    size
}

/// Conservative bound on everything this node may paint (module docs):
/// rect ∪ text-run bound, inflated by border-half + AA margin.
fn paint_bbox(n: &Node, r: Rect, parent: Rect) -> Rect {
    let mut b = r;
    if let Some(text) = n.str_attr("text")
        && !text.is_empty()
    {
        let size = font_size_bound(n);
        // Advance per glyph stays under ~1 em for the supported fonts; ×3/2
        // is the slack that makes it a bound, not a measurement. Height:
        // ascent−descent runs ~1.2 em; 2 em bounds it.
        let wb = text.chars().count() as u32 * size * 3 / 2;
        let hb = 2 * size;
        let run = if n.str_attr("align").as_deref() == Some("center") {
            // Centred in the PARENT (the cases.py button-label contract):
            // bound symmetrically about the parent centre.
            let cx = parent.x + parent.w as i32 / 2;
            let cy = parent.y + parent.h as i32 / 2;
            Rect {
                x: cx - (wb / 2) as i32,
                y: cy - (hb / 2) as i32,
                w: wb,
                h: hb,
            }
        } else {
            // Top-left anchored at the node rect; mark widgets
            // (radio/checkbox) indent the run by mark + gap ≤ h + 8.
            Rect {
                x: r.x,
                y: r.y,
                w: r.w.max(wb + r.h + 8),
                h: r.h.max(hb),
            }
        };
        b = b.union(run);
    }
    let bw = n.u32_attr("border_width", n.u32_attr("style_border_width", 0));
    b.inflate(AA_MARGIN + bw.div_ceil(2))
}

/// Coalesce intersecting-or-touching rects (their union is a superset —
/// always safe) to a fixpoint. O(n²) per pass; n is subtree-granular and
/// small.
fn merge(rects: &mut Vec<Rect>) {
    loop {
        let mut merged_any = false;
        'pass: for i in 0..rects.len() {
            for j in (i + 1)..rects.len() {
                if rects[i].touches(rects[j]) {
                    rects[i] = rects[i].union(rects[j]);
                    rects.swap_remove(j);
                    merged_any = true;
                    break 'pass;
                }
            }
        }
        if !merged_any {
            return;
        }
    }
}

impl Request {
    /// Incremental repaint: bring a frame of `prev`'s pixels up to `self`'s,
    /// touching ONLY the dirty regions. `buf` must hold a full `w×h` RGB888
    /// frame rendered from `prev` (same `stride`); each merged dirty rect
    /// then renders through THE band entry point (`area = rect` — invariant
    /// I1: incremental repaint IS banded repaint) directly into the frame.
    /// Byte-identical to a full render of `self` — `tests/dirty_rects.rs`.
    ///
    /// Returned stats aggregate the per-rect renders;
    /// [`RenderStats::dirty_area_px`] = Σ merged rect areas (0 when nothing
    /// changed — `buf` is then untouched). Glyph-cache counters are the
    /// caller's [`Fonts`] cumulative values, as ever.
    pub fn render_incremental(
        &self,
        prev: &Request,
        fonts: &mut Fonts,
        assets: &Assets,
        buf: &mut [u8],
        stride: usize,
    ) -> Result<RenderStats, RenderError> {
        let mut total = RenderStats::default();
        for r in dirty_rects(prev, self) {
            let off = r.y as usize * stride + r.x as usize * 3;
            let end = off + stride * (r.h as usize - 1) + r.w as usize * 3;
            let stats = self.render_with(fonts, assets, r, &mut buf[off..end], stride)?;
            total.pixels_written += stats.pixels_written;
            for (acc, v) in total
                .pixels_by_class
                .iter_mut()
                .zip(stats.pixels_by_class.iter())
            {
                *acc += v;
            }
            total.bands_rendered += stats.bands_rendered;
            // Fonts-lifetime cumulative counters: latest snapshot wins.
            total.glyphs_rasterized = stats.glyphs_rasterized;
            total.glyph_cache_entries = stats.glyph_cache_entries;
            total.glyph_cache_bytes = stats.glyph_cache_bytes;
            total.dirty_area_px += r.w as u64 * r.h as u64;
        }
        Ok(total)
    }
}
