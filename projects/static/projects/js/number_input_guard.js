/*
 * Number-input wheel guard. Loaded once by each page shell (base.html,
 * admin/admin_base.html, subadmin/subadmin_base.html).
 *
 * THE PROBLEM. When an <input type="number"> has focus, a mouse wheel or trackpad
 * scroll over it changes its value by one step. Someone who types an amount and then
 * scrolls down the page can change the number they just typed without noticing.
 *
 * THE FIX. Blur the input as the wheel event arrives. Once the input has lost focus
 * the browser does not step it, and the scroll carries on to the page. We blur rather
 * than call preventDefault(), because preventDefault would also stop the page from
 * scrolling. That also lets the listener be passive, so it cannot slow scrolling down.
 *
 * ONE LISTENER ON document, NOT ONE PER INPUT. Some rows are created by JavaScript
 * after the page loads (delivery_challan_create's "add line", opex_boq_entry's sheet).
 * A per-input listener would miss those rows. A listener on document catches the event
 * from any input, whenever it was added.
 */
(function () {
  document.addEventListener('wheel', function (event) {
    var target = event.target;
    if (target instanceof HTMLInputElement &&
        target.type === 'number' &&
        target === document.activeElement) {
      target.blur();
    }
  }, { passive: true });
})();
