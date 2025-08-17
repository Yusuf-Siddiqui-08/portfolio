'use strict';

document.addEventListener('DOMContentLoaded', () => {
  // Set current year if footer element exists
  const yearEl = document.getElementById('year');
  if (yearEl) {
    yearEl.textContent = new Date().getFullYear();
  }

  // Initialize carousels
  initCarousels();

  // Initialize button ripple/splash tracking
  initButtonRipples();

  // Wire up search
  initRepoSearch();

  // Initial load
  fetchRepos();
});

// Carousel functionality
function initCarousels() {
  function initCarousel(root) {
    const track = root.querySelector('.carousel-track');
    const slides = Array.from(root.querySelectorAll('.slide'));
    const prevBtn = root.querySelector('.carousel-btn.prev');
    const nextBtn = root.querySelector('.carousel-btn.next');
    if (!track || slides.length === 0) return;
    let index = 0;
    function update() {
      track.style.transform = 'translateX(' + (-index * 100) + '%)';
    }
    function prev() { index = (index - 1 + slides.length) % slides.length; update(); }
    function next() { index = (index + 1) % slides.length; update(); }
    if (prevBtn) prevBtn.addEventListener('click', prev);
    if (nextBtn) nextBtn.addEventListener('click', next);
    // Keyboard support when carousel receives focus
    root.setAttribute('tabindex', '0');
    root.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowLeft') { prev(); }
      if (e.key === 'ArrowRight') { next(); }
    });
    update();
  }
  document.querySelectorAll('[data-carousel]').forEach(initCarousel);
}

// Button ripple hover effect: track mouse position within each .btn and set CSS vars
function initButtonRipples() {
  const buttons = Array.from(document.querySelectorAll('.btn'));
  buttons.forEach((btn) => {
    if (btn.dataset.rippleInit === '1') return;
    btn.dataset.rippleInit = '1';
    
    // Create ripple element for liquid glass effect
    if (!btn.querySelector('.ripple')) {
      const ripple = document.createElement('span');
      ripple.className = 'ripple';
      btn.appendChild(ripple);
    }
    
    // Wrap inner HTML to keep content above the ripple
    if (!btn.querySelector('.btn-content')) {
      const span = document.createElement('span');
      span.className = 'btn-content';
      while (btn.firstChild && btn.firstChild !== btn.querySelector('.ripple')) {
        span.appendChild(btn.firstChild);
      }
      btn.appendChild(span);
    }
    
    btn.addEventListener('mousemove', (e) => {
      const rect = btn.getBoundingClientRect();
      // Compute a scale large enough to cover button diagonal
      const maxDim = Math.max(rect.width, rect.height);
      const diag = Math.sqrt(rect.width * rect.width + rect.height * rect.height);
      const scale = (diag / 16) + 2; // 16 is base ripple size; add padding
      btn.style.setProperty('--r-scale', String(Math.ceil(scale)));
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      btn.style.setProperty('--mouse-x', x + 'px');
      btn.style.setProperty('--mouse-y', y + 'px');
      // Keep ripple fully expanded while hovered
      btn.classList.add('rippling');
    });
    // On keyboard focus, center the splash
    btn.addEventListener('focus', () => {
      btn.style.setProperty('--mouse-x', '50%');
      btn.style.setProperty('--mouse-y', '50%');
    });
    // Clean up vars when leaving
    btn.addEventListener('mouseleave', () => {
      // Let ripple remain at last position and fade out smoothly even off-button
      btn.classList.remove('rippling');
      btn.classList.add('rippling-out');
      if (btn._rippleCleanupTimer) {
        clearTimeout(btn._rippleCleanupTimer);
      }
      btn._rippleCleanupTimer = window.setTimeout(() => {
        btn.classList.remove('rippling-out');
        btn.style.removeProperty('--mouse-x');
        btn.style.removeProperty('--mouse-y');
        btn.style.removeProperty('--r-scale');
        btn._rippleCleanupTimer = null;
      }, 500);
    });
  });
}

// Image Modal Functions
function openImageModal(src, alt) {
  const modal = document.getElementById('imageModal');
  const modalImg = document.getElementById('modalImage');
  const caption = document.getElementById('modalCaption');

  modal.style.display = 'block';
  modalImg.src = src;
  modalImg.alt = alt;
  caption.textContent = alt;

  // Prevent body scrolling when modal is open
  document.body.style.overflow = 'hidden';
}

function closeImageModal() {
  const modal = document.getElementById('imageModal');
  modal.style.display = 'none';

  // Restore body scrolling
  document.body.style.overflow = 'auto';
}

// Close modal on Escape key
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    closeImageModal();
  }
});

// Make functions globally available for onclick handlers
window.openImageModal = openImageModal;
window.closeImageModal = closeImageModal;

async function fetchRepos() {
  const statusEl = document.getElementById('status');
  const container = document.getElementById('repo-container');
  if (!statusEl || !container) return;

  try {
    statusEl.textContent = 'Loading repositories…';
    const response = await fetch('/api/github/repos');
    if (!response.ok) throw new Error('Request failed: ' + response.status);
    const json = await response.json();
    if (!json.ok || !Array.isArray(json.repos)) throw new Error(json.message || 'Invalid response');

    statusEl.textContent = '';
    renderRepos(json.repos, container);
  } catch (err) {
    console.error(err);
    statusEl.textContent = 'Failed to load repositories. Please try again later.';
  }
}

async function fetchSearch(q) {
  const statusEl = document.getElementById('status');
  const container = document.getElementById('repo-container');
  if (!statusEl || !container) return;

  try {
    if (!q) { fetchRepos(); return; }
    statusEl.textContent = 'Searching…';
    const response = await fetch('/api/search?q=' + encodeURIComponent(q));
    if (!response.ok) throw new Error('Search failed: ' + response.status);
    const json = await response.json();
    if (!json.ok || !Array.isArray(json.results)) throw new Error(json.message || 'Invalid response');

    statusEl.textContent = json.count === 0 ? 'No results.' : '';
    renderRepos(json.results, container);
  } catch (err) {
    console.error(err);
    statusEl.textContent = 'Search failed. Please try again later.';
  }
}

function renderRepos(repos, container) {
  container.innerHTML = '';
  const fragment = document.createDocumentFragment();

  repos.forEach(repo => {
    const name = repo.name;
    const url = repo.html_url;
    const description = repo.description || 'No description provided.';
    const language = repo.language || '';
    const stars = repo.stargazers_count ?? repo.stars ?? 0;
    const forks = repo.forks_count ?? repo.forks ?? 0;
    const updatedAt = repo.updated_at || repo.pushed_at || '';

    const card = document.createElement('article');
    card.className = 'repo-card';

    const link = document.createElement('a');
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.className = 'btn repo-button';
    link.innerHTML = `
      <span aria-hidden="true" style="display:inline-flex;align-items:center;gap:8px;">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M12 .5C5.73.5.98 5.24.98 11.52c0 4.86 3.15 8.99 7.51 10.45.55.1.75-.24.75-.54 0-.27-.01-1.16-.02-2.1-3.06.67-3.71-1.3-3.71-1.3-.5-1.27-1.23-1.61-1.23-1.61-.99-.68.08-.66.08-.66 1.09.08 1.66 1.12 1.66 1.12.98 1.67 2.58 1.19 3.2.91.1-.71.38-1.19.69-1.46-2.44-.28-5-1.22-5-5.44 0-1.2.43-2.19 1.13-2.97-.11-.28-.49-1.41.11-2.94 0 0 .93-.3 3.05 1.13a10.6 10.6 0 0 1 2.78-.37c.94 0 1.88.13 2.77.37 2.12-1.43 3.05-1.13 3.05-1.13.6 1.53.22 2.66.11 2.94.7.78 1.13 1.77 1.13 2.97 0 4.23-2.56 5.16-5 5.44.39.33.74.98.74 1.98 0 1.43-.01 2.58-.01 2.93 0 .3.2.65.76.54 4.35-1.46 7.5-5.59 7.5-10.45C23.02 5.24 18.27.5 12 .5z"/>
        </svg>
        <span>${name}</span>
      </span>
    `;

    const desc = document.createElement('p');
    desc.className = 'note';
    desc.style.margin = '10px 0 12px 0';
    desc.textContent = description;

    const meta = document.createElement('div');
    meta.style.display = 'flex';
    meta.style.flexWrap = 'wrap';
    meta.style.gap = '10px';
    meta.style.fontSize = '14px';
    meta.style.color = 'var(--text-dim)';

    const pieces = [];
    if (language) pieces.push('Language: ' + language);
    pieces.push('Stars: ' + stars);
    if (forks) pieces.push('Forks: ' + forks);
    if (updatedAt) {
      const d = new Date(updatedAt);
      if (!isNaN(d)) pieces.push('Updated: ' + d.toLocaleDateString());
    }
    meta.textContent = pieces.join(' • ');

    card.appendChild(link);
    card.appendChild(desc);
    card.appendChild(meta);
    fragment.appendChild(card);
  });

  container.appendChild(fragment);
  // Initialize ripple behavior for newly added repo buttons
  initButtonRipples();
}

function initRepoSearch() {
  const input = document.getElementById('repo-search');
  if (!input) return;

  const debounced = debounce((val) => {
    fetchSearch(val.trim());
  }, 300);

  input.addEventListener('input', (e) => {
    debounced(e.target.value || '');
  });
}

function debounce(fn, wait) {
  let t = null;
  return function debounced(...args) {
    if (t) clearTimeout(t);
    t = setTimeout(() => fn.apply(this, args), wait);
  }
}
