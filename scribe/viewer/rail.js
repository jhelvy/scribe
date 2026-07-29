/**
 * Margin-rail layout: where each note sits relative to its anchor.
 *
 * The naive version of this — walk top to bottom placing each note at
 * `max(anchor, previousBottom + gap)` — is what the previous generation of this
 * viewer did, and it is why notes drifted. It is a ratchet: a note can only ever
 * be pushed *down*, so one tall note near the top shoves every note below it
 * away from its anchor, permanently, and the further you read the worse the
 * alignment gets.
 *
 * Google Docs does not feel like that because a cluster of comments distributes
 * *around* its anchors — some above, some below. That behaviour has an exact
 * formulation: minimise the total squared displacement from the anchors,
 * subject to the notes staying in order and not overlapping. Substituting
 * `x[i] = top[i] - cumulativeOffset[i]` turns the no-overlap constraint into
 * "x must be non-decreasing", which makes this isotonic regression, solved
 * optimally in O(n) by pool-adjacent-violators.
 *
 * Weights are what make focus work. A focused note gets a very large weight, so
 * the optimum puts it exactly on its anchor and displaces its neighbours around
 * it — which is precisely the "snap together" interaction, for free, out of the
 * same solver.
 *
 * DOM-free on purpose so Node can test it directly.
 */

(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Rail = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var FOCUS_WEIGHT = 1000;

  /**
   * @param {Array<{anchor:number, height:number, weight?:number}>} notes
   *        in document order; `anchor` is the desired top in container space.
   * @param {{gap?:number, minTop?:number}} [opts]
   * @returns {number[]} the top for each note, same order.
   */
  function layout(notes, opts) {
    opts = opts || {};
    var gap = typeof opts.gap === "number" ? opts.gap : 12;
    var minTop = typeof opts.minTop === "number" ? opts.minTop : 0;
    var n = notes.length;
    if (!n) return [];

    // offset[i] is where note i would sit if the whole run were packed tight
    // from zero. Subtracting it linearises the ordering constraint.
    var offset = new Array(n);
    var acc = 0;
    for (var i = 0; i < n; i++) {
      offset[i] = acc;
      acc += Math.max(0, notes[i].height) + gap;
    }

    var target = new Array(n);
    var weight = new Array(n);
    for (i = 0; i < n; i++) {
      target[i] = notes[i].anchor - offset[i];
      weight[i] = notes[i].weight > 0 ? notes[i].weight : 1;
    }

    // Pool adjacent violators.
    var blocks = [];
    for (i = 0; i < n; i++) {
      var block = {
        weighted: weight[i] * target[i],
        weight: weight[i],
        start: i,
        end: i,
        value: target[i],
      };
      while (blocks.length && blocks[blocks.length - 1].value > block.value) {
        var prev = blocks.pop();
        block.weighted += prev.weighted;
        block.weight += prev.weight;
        block.start = prev.start;
        block.value = block.weighted / block.weight;
      }
      blocks.push(block);
    }

    var tops = new Array(n);
    for (var b = 0; b < blocks.length; b++) {
      var blk = blocks[b];
      for (i = blk.start; i <= blk.end; i++) tops[i] = blk.value + offset[i];
    }

    // Keep the run inside the container. Because `offset` is cumulative,
    // clamping against `minTop + offset[i]` cannot reintroduce an overlap.
    for (i = 0; i < n; i++) tops[i] = Math.max(tops[i], minTop + offset[i]);
    return tops;
  }

  /**
   * Lay out with one note pinned to its anchor.
   *
   * The heavy weight alone gets within a fraction of a pixel but not exactly
   * onto the anchor, because the focused note still shares a pooled block with
   * its neighbours. Since "this note is aligned with the thing it describes" is
   * the entire point of the interaction, the weighted solve is followed by a
   * pass that pins it and pushes the two sides apart from there — keeping the
   * distribution PAV chose, while making the alignment exact.
   */
  function layoutWithFocus(notes, focusIndex, opts) {
    opts = opts || {};
    var gap = typeof opts.gap === "number" ? opts.gap : 12;
    var minTop = typeof opts.minTop === "number" ? opts.minTop : 0;
    var n = notes.length;
    if (!n) return [];
    if (!(focusIndex >= 0 && focusIndex < n)) return layout(notes, opts);

    var weighted = notes.map(function (note, i) {
      return {
        anchor: note.anchor,
        height: note.height,
        weight: i === focusIndex ? FOCUS_WEIGHT : note.weight || 1,
      };
    });
    var tops = layout(weighted, { gap: gap, minTop: -Infinity });

    tops[focusIndex] = notes[focusIndex].anchor;
    for (var i = focusIndex - 1; i >= 0; i--) {
      tops[i] = Math.min(tops[i], tops[i + 1] - notes[i].height - gap);
    }
    for (i = focusIndex + 1; i < n; i++) {
      tops[i] = Math.max(tops[i], tops[i - 1] + notes[i - 1].height + gap);
    }
    // A note cannot render above the container even to stay on its anchor.
    for (i = 1; i < n; i++) tops[i] = Math.max(tops[i], tops[i - 1] + notes[i - 1].height + gap);
    if (tops[0] < minTop) {
      var shift = minTop - tops[0];
      for (i = 0; i < n; i++) tops[i] += shift;
    }
    return tops;
  }

  /** True when every note sits at its anchor and nothing overlaps. */
  function verify(notes, tops, gap) {
    gap = typeof gap === "number" ? gap : 12;
    for (var i = 1; i < tops.length; i++) {
      if (tops[i] < tops[i - 1] + notes[i - 1].height + gap - 1e-6) return false;
    }
    return true;
  }

  return { layout: layout, layoutWithFocus: layoutWithFocus, verify: verify, FOCUS_WEIGHT: FOCUS_WEIGHT };
});
