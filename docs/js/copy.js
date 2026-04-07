function copyInstall() {
  var text = 'npx skills add mme/actor.sh -g';
  var tooltip = document.getElementById('copy-tooltip');

  function flash() {
    if (!tooltip) return;
    tooltip.classList.add('show');
    setTimeout(function () {
      tooltip.classList.remove('show');
    }, 1600);
  }

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(flash).catch(fallback);
  } else {
    fallback();
  }

  function fallback() {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0;top:0;left:0;pointer-events:none;';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    flash();
  }
}
