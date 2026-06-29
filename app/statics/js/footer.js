window.renderSiteFooter = async function renderSiteFooter() {
  if (document.querySelector('.site-footer')) return;

  const footer = document.createElement('div');
  footer.className = 'site-footer';
  footer.setAttribute('aria-hidden', 'true');

  const link = (text, href) => {
    const node = document.createElement('a');
    node.href = href;
    node.target = '_blank';
    node.rel = 'noopener';
    node.textContent = text;
    return node;
  };

  const brand = link('Fiveonine', 'https://github.com/Fiveo9');
  footer.appendChild(brand);

  document.body.appendChild(footer);
};

const _bootSiteFooter = () => {
  if (typeof window.renderSiteFooter === 'function') {
    void window.renderSiteFooter();
  }
};

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _bootSiteFooter, { once: true });
} else {
  _bootSiteFooter();
}

window.addEventListener('pageshow', _bootSiteFooter);
