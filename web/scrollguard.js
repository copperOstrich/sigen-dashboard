// Keeps the view still when the page shrinks for a moment. A panel that hides and comes back (its
// data blipped) makes the page shorter for an instant; at the very bottom, Safari then pulls the
// view up by that much and leaves it there when the panel returns (it doesn't anchor the scroll
// like Chrome does). So when the content gets shorter while you're near the bottom, the page keeps
// its old height and you're put back where you were, before anything is drawn. It lets go once the
// content is back, you've scrolled away from the bottom, or a few minutes have passed.
(function () {
  const wrap = document.querySelector(".wrap"), grid = wrap && wrap.querySelector(".grid");
  if (!grid || !window.ResizeObserver) return;
  let natural = grid.offsetHeight, wrapH = wrap.offsetHeight;   // the content's own height, and the page's
  let heldFor = 0, since = 0, width = window.innerWidth;
  // where you were before the change (scroll events from Safari's pull-up arrive after we've seen it)
  let lastY = window.scrollY, lastMax = document.documentElement.scrollHeight - window.innerHeight;
  const nearBottom = (y, max) => y > max - 300;
  // after letting go, the browser may still nudge the view as the panel settles: for a moment, a
  // move you didn't make (no touch, wheel or key) is put back
  let keepY = null, keepUntil = 0, touched = 0;
  ["touchstart", "touchmove", "wheel", "keydown", "mousedown"].forEach((ev) =>
    window.addEventListener(ev, () => { touched = Date.now(); }, { passive: true }));
  const release = (hold = true) => {
    wrap.style.minHeight = ""; heldFor = 0;
    if (hold) { keepY = window.scrollY; keepUntil = Date.now() + 1500; }
  };

  new ResizeObserver(() => {
    const h = grid.offsetHeight;
    if (heldFor && h >= heldFor - 20) release();                  // it came back
    else if (!heldFor && h < natural - 20 && nearBottom(lastY, lastMax) && !document.body.classList.contains("wall-mode")) {
      heldFor = natural; since = Date.now();
      wrap.style.minHeight = wrapH + "px";
      if (Math.abs(window.scrollY - lastY) > 2) window.scrollTo(0, lastY);
    }
    if (!heldFor) { natural = h; wrapH = wrap.offsetHeight; }
  }).observe(grid);

  window.addEventListener("scroll", () => {
    if (keepY != null && Date.now() < keepUntil && Date.now() - touched > 1000 && Math.abs(window.scrollY - keepY) > 2) {
      window.scrollTo(0, keepY);
      return;
    }
    if (keepY != null && Date.now() >= keepUntil) keepY = null;
    lastY = window.scrollY; lastMax = document.documentElement.scrollHeight - window.innerHeight;
    if (heldFor && (!nearBottom(lastY, lastMax) || Date.now() - since > 5 * 60000)) release(false);
  }, { passive: true });
  setInterval(() => { if (heldFor && Date.now() - since > 5 * 60000) release(false); }, 30000);
  // turning the phone or resizing the window is a real change (the toolbar coming and going isn't)
  window.addEventListener("resize", () => {
    lastMax = document.documentElement.scrollHeight - window.innerHeight;
    if (window.innerWidth === width) return;
    width = window.innerWidth; release(false);
    natural = grid.offsetHeight; wrapH = wrap.offsetHeight;
  });
})();
