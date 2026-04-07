/*
Extracted from:
  /Users/mme/Projects/cmdlane-go/main/cmd/cmdlane/rest_oauth_theme.go

Original CSS used by the OAuth success page:

* {
  box-sizing: border-box;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Monaco, Consolas,
    "Liberation Mono", "Courier New", monospace;
}
html, body {
  margin: 0;
  padding: 0;
  height: 100%;
  overflow: hidden;
  background: #ffffff;
  color: #000000;
}
#background {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  white-space: pre;
  color: #cccccc;
  z-index: 0;
  overflow: hidden;
}

The original OAuth page also called:
  setTimeout(function() { window.close(); }, 5000);
That behavior is omitted here so this file only contains the animation.
*/

(function () {
  function start() {
    var styleId = "cmdlane-oauth-animation-style";
    if (!document.getElementById(styleId)) {
      var style = document.createElement("style");
      style.id = styleId;
      style.textContent = [
        '* { box-sizing: border-box; font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }',
        "html, body { margin: 0; padding: 0; height: 100%; overflow: hidden; background: #ffffff; color: #000000; }",
        "#background { position: fixed; top: 0; left: 0; width: 100%; height: 100%; white-space: pre; color: #cccccc; z-index: 0; overflow: hidden; }",
      ].join("\n");
      document.head.appendChild(style);
    }

    var bg = document.getElementById("background");
    if (!bg) {
      bg = document.createElement("div");
      bg.id = "background";
      document.body.prepend(bg);
    }

    var texture = " .,:;-~=+*x!tiLC0O8%#@";
    var states = [
      [0, 0],
      [1, 3],
      [2, 4],
      [4, 2],
      [3, 5],
      [6, 1],
      [4, 4],
      [2, 7],
    ];
    var cycleSeconds = 7.5;
    var spatialRange = 3.0;

    function factorial(n) {
      if (n <= 1) return 1;
      var f = 1;
      for (var i = 2; i <= n; i++) f *= i;
      return f;
    }

    function hermiteH(n, x) {
      if (n === 0) return 1;
      if (n === 1) return 2 * x;
      var hm2 = 1;
      var hm1 = 2 * x;
      for (var k = 2; k <= n; k++) {
        var h = 2 * x * hm1 - 2 * (k - 1) * hm2;
        hm2 = hm1;
        hm1 = h;
      }
      return hm1;
    }

    function psi1D(n, x) {
      var norm = 1 / Math.sqrt(Math.pow(2, n) * factorial(n) * Math.sqrt(Math.PI));
      return norm * Math.exp(-0.5 * x * x) * hermiteH(n, x);
    }

    function psi2D(nx, ny, x, y) {
      return psi1D(nx, x) * psi1D(ny, y);
    }

    var cols = 0;
    var rows = 0;
    var startTime = Date.now();

    function resize() {
      var charW = 8.4;
      var charH = 16.8;
      cols = Math.ceil(window.innerWidth / charW) + 1;
      rows = Math.ceil(window.innerHeight / charH) + 1;
    }

    function render() {
      if (cols <= 0 || rows <= 0) return;

      var t = (Date.now() - startTime) / 1000;
      var idx = Math.floor(t / cycleSeconds) % states.length;
      var next = (idx + 1) % states.length;
      var local = t / cycleSeconds - Math.floor(t / cycleSeconds);
      var s = 0.5 - 0.5 * Math.cos(local * Math.PI);

      var aspect = cols / Math.max(1, rows);
      var numTexture = texture.length;
      var lines = [];

      for (var row = 0; row < rows; row++) {
        var line = "";
        for (var col = 0; col < cols; col++) {
          var xNorm = ((col + 0.5) / cols) * 2 - 1;
          var yNorm = ((row + 0.5) / rows) * 2 - 1;
          var x = xNorm * spatialRange;
          var y = (yNorm * spatialRange) / aspect;

          var a = psi2D(states[idx][0], states[idx][1], x, y);
          var b = psi2D(states[next][0], states[next][1], x, y);
          var amp = (1 - s) * a + s * b;

          var intensity = amp * amp;
          intensity = Math.pow(intensity * 18.0, 0.65);
          if (intensity < 0) intensity = 0;
          if (intensity > 1) intensity = 1;

          var idxRune = Math.floor(intensity * (numTexture - 1));
          if (idxRune < 0) idxRune = 0;
          if (idxRune >= numTexture) idxRune = numTexture - 1;
          line += texture[idxRune];
        }
        lines.push(line);
      }

      bg.textContent = lines.join("\n");
    }

    function animate() {
      render();
      window.requestAnimationFrame(animate);
    }

    resize();
    window.addEventListener("resize", resize);
    animate();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
    return;
  }

  start();
})();
