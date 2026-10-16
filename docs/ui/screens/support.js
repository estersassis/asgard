/*
 * Minimal preview runtime for the ASGARD UI mockups (*.dc.html).
 *
 * The screens were drawn in a design canvas that uses a small template
 * format: {{path}} holes, <sc-for>, <sc-if>, and a `Component` class with
 * renderVals(). This file renders that format in a plain browser so the
 * mockups can be opened locally (file:// or any static server).
 *
 * It is a viewer for the design reference only. Nothing in asgard/ui should
 * import or copy it.
 *
 * Query string: ?theme=light switches the mockup to the light palette.
 */
(function () {
  'use strict';

  function DCLogic(props) {
    this.props = props || {};
    this.state = {};
  }
  DCLogic.prototype.setState = function (patch) {
    Object.assign(this.state, typeof patch === 'function' ? patch(this.state) : patch);
    if (this.__render) this.__render();
  };
  DCLogic.prototype.forceUpdate = function () {
    if (this.__render) this.__render();
  };
  window.DCLogic = DCLogic;

  var WHOLE = /^\s*\{\{\s*([^}]+?)\s*\}\}\s*$/;
  var HOLE = /\{\{\s*([^}]+?)\s*\}\}/g;

  function lookup(path, scope) {
    if (path === 'true') return true;
    if (path === 'false') return false;
    if (path === 'null') return null;
    if (/^-?\d+(\.\d+)?$/.test(path)) return Number(path);
    var parts = path.split('.');
    var value = scope;
    for (var i = 0; i < parts.length; i++) {
      if (value == null) return undefined;
      value = value[parts[i]];
    }
    return value;
  }

  function interpolate(text, scope) {
    return text.replace(HOLE, function (_, path) {
      var v = lookup(path, scope);
      return v == null ? '' : String(v);
    });
  }

  var EVENT_ATTR = /^on[a-z]+$/;

  function renderNode(node, scope, out) {
    if (node.nodeType === Node.TEXT_NODE) {
      out.appendChild(document.createTextNode(interpolate(node.nodeValue, scope)));
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    var tag = node.tagName.toLowerCase();

    if (tag === 'sc-for') {
      var list = lookup((node.getAttribute('list') || '').replace(/[{}\s]/g, ''), scope) || [];
      var as = node.getAttribute('as') || 'item';
      list.forEach(function (item, index) {
        var child = Object.create(scope);
        child[as] = item;
        child.$index = index;
        renderChildren(node, child, out);
      });
      return;
    }
    if (tag === 'sc-if') {
      var cond = lookup((node.getAttribute('value') || '').replace(/[{}\s]/g, ''), scope);
      if (cond) renderChildren(node, scope, out);
      return;
    }
    if (tag === 'dc-import' || tag === 'helmet') return;

    var el = document.createElementNS(node.namespaceURI, node.localName);
    Array.prototype.forEach.call(node.attributes, function (attr) {
      var name = attr.name;
      var whole = attr.value.match(WHOLE);
      if (EVENT_ATTR.test(name)) {
        var fn = whole ? lookup(whole[1], scope) : null;
        if (typeof fn === 'function') el.addEventListener(name.slice(2), fn);
        return;
      }
      var value = whole ? lookup(whole[1], scope) : interpolate(attr.value, scope);
      if (name === 'disabled' || name === 'checked') {
        if (value === true || value === 'true') el.setAttribute(name, '');
        return;
      }
      if (name === 'defaultvalue') {
        el.__defaultValue = value == null ? '' : String(value);
        return;
      }
      if (name === 'value' && (tag === 'input' || tag === 'select' || tag === 'textarea')) {
        el.__value = value == null ? '' : String(value);
        return;
      }
      el.setAttribute(name, value == null ? '' : String(value));
    });
    renderChildren(node.content || node, scope, el);
    var v = el.__value != null ? el.__value : el.__defaultValue;
    if (v != null) el.value = v;
    out.appendChild(el);
  }

  function renderChildren(parent, scope, out) {
    Array.prototype.forEach.call(parent.childNodes, function (child) {
      renderNode(child, scope, out);
    });
  }

  function propsFrom(script) {
    var props = {};
    var declared = {};
    try { declared = JSON.parse(script.getAttribute('data-props') || '{}'); } catch (e) { declared = {}; }
    Object.keys(declared).forEach(function (key) {
      if (key.charAt(0) !== '$' && declared[key] && 'default' in declared[key]) props[key] = declared[key]['default'];
    });
    var query = new URLSearchParams(window.location.search);
    query.forEach(function (value, key) { props[key] = value; });
    return props;
  }

  function boot() {
    var host = document.querySelector('x-dc');
    var script = document.querySelector('script[data-dc-script]');
    if (!host || !script) return;

    var helmet = host.querySelector('helmet');
    if (helmet) {
      Array.prototype.forEach.call(helmet.children, function (child) {
        document.head.appendChild(child.cloneNode(true));
      });
    }

    var template = document.createElement('div');
    Array.prototype.forEach.call(host.childNodes, function (child) {
      template.appendChild(child.cloneNode(true));
    });
    host.innerHTML = '';
    host.style.display = 'block';

    /* eslint-disable no-new-func */
    var Component = new Function('DCLogic', script.textContent + '\n;return Component;')(DCLogic);
    var instance = new Component(propsFrom(script));
    instance.__render = function () {
      var focusedId = document.activeElement && document.activeElement.getAttribute('aria-label');
      var frag = document.createDocumentFragment();
      renderChildren(template, instance.renderVals(), frag);
      host.innerHTML = '';
      host.appendChild(frag);
      if (focusedId) {
        var again = host.querySelector('[aria-label="' + focusedId + '"]');
        if (again) again.focus();
      }
    };
    instance.__render();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
