/* Small browser boundary: key storage and authenticated native-form posts. */
(function () {
  "use strict";

  var storageKey = "asrsub-key";

  function currentKey() {
    return window.sessionStorage.getItem(storageKey) || "";
  }

  function paintKeyState() {
    var state = document.getElementById("key-state");
    if (!state) return;
    var hasKey = !!currentKey();
    state.textContent = hasKey ? "key set" : "no key";
    state.classList.toggle("ok", hasKey);
  }

  function unlock() {
    var input = document.getElementById("ctl-key");
    if (!input) return;
    var value = input.value.trim();
    if (value) {
      window.sessionStorage.setItem(storageKey, value);
    } else {
      window.sessionStorage.removeItem(storageKey);
    }
    input.value = "";
    paintKeyState();
  }

  function encodeForm(form) {
    var params = new URLSearchParams();
    Array.prototype.forEach.call(form.elements, function (element) {
      if (!element.name || element.disabled) return;
      if ((element.type === "checkbox" || element.type === "radio") && !element.checked) return;
      params.append(element.name, element.value);
    });
    return params.toString();
  }

  function submitAuthenticated(event) {
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
    var headers = { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" };
    var key = currentKey();
    if (key) headers["X-API-Key"] = key;

    window.fetch(target.pathname + target.search, {
      method: "POST",
      headers: headers,
      body: encodeForm(form),
      redirect: "follow"
    }).then(function (response) {
      return response.text();
    }).then(function (html) {
      // Successful 303 responses are followed by fetch. Error responses are
      // complete HTML documents with their original status, so both paths use
      // same document replacement and never swap a partial response.
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
    var button = document.querySelector("[data-key-action='unlock']");
    if (button) button.addEventListener("click", unlock);
    var input = document.getElementById("ctl-key");
    if (input) input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        unlock();
      }
    });
    Array.prototype.forEach.call(document.querySelectorAll("form[data-authenticated]"), function (form) {
      form.addEventListener("submit", submitAuthenticated);
    });
    paintKeyState();
  });
})();
