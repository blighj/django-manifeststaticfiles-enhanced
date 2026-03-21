'use strict';
{
  const globals = this || globalThis || window || self || {};
  const django = globals.django || (globals.django = {});

  // Try to get the STATIC_URL from the script tag or fall back to "/static/"
  let STATIC_URL = "/static/";

  // Find the script tag with ID "staticjs-static-url"
  const scriptTag = document.getElementById("staticjs-static-url");
  if (scriptTag && scriptTag.dataset && scriptTag.dataset.staticUrl) {
    STATIC_URL = scriptTag.dataset.staticUrl;
  }

  /**
   * A simple function that returns the asset path in static_root in debug mode.
   * In production, this function will be replaced with a version that maps assets to their hashed paths.
   *
   * @param {string} asset - The path to the static asset
   * @returns {string} The static path
   */
  django.static = function(asset) {
    return `${STATIC_URL}${asset}`;
  };
}