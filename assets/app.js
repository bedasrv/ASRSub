/* Small browser helper for ordinary same-origin form posts. */
(function () {
  "use strict";

  function encodeForm(form) {
    var params = new URLSearchParams();
    Array.prototype.forEach.call(form.elements, function (element) {
      if (!element.name || element.disabled) return;
      if ((element.type === "checkbox" || element.type === "radio") && !element.checked) return;
      params.append(element.name, element.value);
    });
    return params.toString();
  }

  function submitForm(event) {
    event.preventDefault();
    var form = event.currentTarget;
    var controls = form.querySelectorAll("button, input, select, textarea");
    Array.prototype.forEach.call(controls, function (control) {
      control.disabled = true;
    });

    var target = new URL(form.getAttribute("action"), window.location.origin);
    if (target.origin !== window.location.origin) {
      throw new Error("cross-origin operator action refused");
    }

    window.fetch(target.pathname + target.search, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      body: encodeForm(form),
      redirect: "follow"
    }).then(function (response) {
      return response.text();
    }).then(function (html) {
      // Successful 303 responses are followed by fetch. Error responses are
      // complete HTML documents with their original status.
      document.open();
      document.write(html);
      document.close();
    }).catch(function () {
      Array.prototype.forEach.call(controls, function (control) {
        control.disabled = false;
      });
      window.location.reload();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("form[method='post']"), function (form) {
      form.addEventListener("submit", submitForm);
    });
  });
})();
