/*
 * CENTRA browser helper, shared by every page.
 * - Centra.api(): JSON calls with the X-Centra header the server requires on changes (CSRF guard).
 *   Errors are thrown as {status, code, message, extra} with a message written for people.
 * - Centra.pay(): opens Razorpay Checkout for an order created by the server, then hands the result
 *   back to the server to verify. With mock payments (development only) it shows a confirm box instead.
 */
(function () {
  async function api(method, url, body) {
    const opts = { method, headers: { "X-Centra": "1" }, credentials: "same-origin" };
    if (body instanceof FormData) opts.body = body;
    else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(url, opts); }
    catch (e) { throw { status: 0, code: "network", message: "You appear to be offline. Check your connection and try again." }; }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      const err = (data && data.error) || {};
      const { code, message, ...extra } = err;
      throw { status: res.status, code: code || "error", message: message || "Something went wrong. Please try again.", extra };
    }
    return data;
  }

  let checkoutLoading = null;
  function loadCheckout() {
    if (window.Razorpay) return Promise.resolve();
    if (!checkoutLoading) checkoutLoading = new Promise((ok, fail) => {
      const s = document.createElement("script");
      s.src = "https://checkout.razorpay.com/v1/checkout.js";
      s.onload = ok;
      s.onerror = () => { checkoutLoading = null; fail({ message: "The payment window couldn't load. Check your connection and try again." }); };
      document.head.appendChild(s);
    });
    return checkoutLoading;
  }

  async function pay(order, prefill) {
    if (order.mock) {
      const rupees = (order.amount / 100).toLocaleString("en-IN", { style: "currency", currency: "INR" });
      if (!window.confirm("Development payment (no money moves)\n\n" + order.description + "\n" + rupees + "\n\nPress OK to simulate a successful payment."))
        throw { code: "dismissed", message: "Payment was not completed." };
      return api("POST", "/api/payments/mock-pay", { order_id: order.order_id });
    }
    await loadCheckout();
    return new Promise((resolve, reject) => {
      const rz = new window.Razorpay({
        key: order.key, order_id: order.order_id, amount: order.amount, currency: order.currency || "INR",
        name: order.name || "CENTRA", description: order.description || "", prefill: prefill || {},
        theme: { color: "#59652e" },
        handler: (resp) => api("POST", "/api/payments/verify", {
          order_id: resp.razorpay_order_id, payment_id: resp.razorpay_payment_id, signature: resp.razorpay_signature,
        }).then(resolve, reject),
        modal: { ondismiss: () => reject({ code: "dismissed", message: "Payment was not completed." }) },
      });
      rz.on("payment.failed", (r) => reject({ code: "failed", message: (r && r.error && r.error.description) || "The payment failed. Please try again." }));
      rz.open();
    });
  }

  window.Centra = {
    api,
    get: (u) => api("GET", u),
    post: (u, b) => api("POST", u, b === undefined ? {} : b),
    put: (u, b) => api("PUT", u, b),
    del: (u) => api("DELETE", u),
    upload: (u, file) => { const f = new FormData(); f.append("file", file); return api("POST", u, f); },
    pay,
    signOut: () => api("POST", "/api/auth/signout", {}).finally(() => { window.location.href = "/"; }),
  };
})();
