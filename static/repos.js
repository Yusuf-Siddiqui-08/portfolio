'use strict';

document.addEventListener('DOMContentLoaded', () => {
  // Set current year if footer element exists
  const yearEl = document.getElementById('year');
  if (yearEl) {
    yearEl.textContent = new Date().getFullYear();
  }

  // Initialize theme toggle and sync button text
  initTheme();

  // Initialize blur-up behavior on images
  initBlurUpImages();

  // Initialize carousels
  initCarousels();

  // Initialize button ripple/splash tracking
  initButtonRipples();

  // Initialize typewriter effect for CLI-esque titles
  initTypewriterTitles();

  // Wire up search
  initRepoSearch();

  // Wire up image modal open/close handlers (no inline onclicks)
  initImageModalHandlers();

  // Wire up contact form behavior if present
  initContactForm();

  // Load CAPTCHA vendor scripts dynamically if needed
  initCaptchaLoaders();

  // Initial load for repos list (repos page only)
  fetchRepos();
});

// Blur-up: when images load, remove the blur
function initBlurUpImages() {
  const imgs = Array.from(document.querySelectorAll('img.blur-up'));
  imgs.forEach((img) => {
    const onLoad = () => {
      img.classList.add('is-loaded');
      img.removeEventListener('load', onLoad);
    };
    // If already loaded from cache
    if (img.complete && img.naturalWidth > 0) {
      img.classList.add('is-loaded');
    } else {
      // Prefer decode if supported for smoother transition
      if (typeof img.decode === 'function') {
        img.decode().then(() => onLoad()).catch(() => {
          // Fallback to load event in case decode fails
          img.addEventListener('load', onLoad, { once: true });
        });
      } else {
        img.addEventListener('load', onLoad, { once: true });
      }
    }
  });
}

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
  if (!modal || !modalImg || !caption) return;

  modal.style.display = 'block';
  modalImg.src = src;
  modalImg.alt = alt;
  caption.textContent = alt || '';

  // Prevent body scrolling when modal is open
  document.body.style.overflow = 'hidden';
}

function closeImageModal() {
  const modal = document.getElementById('imageModal');
  if (!modal) return;
  modal.style.display = 'none';

  // Restore body scrolling
  document.body.style.overflow = 'auto';
}

function initImageModalHandlers() {
  // Attach click to any images inside carousels or with .modal-trigger class
  const images = Array.from(document.querySelectorAll('.carousel img, img.modal-trigger'));
  images.forEach(img => {
    if (img.dataset.modalWired === '1') return;
    img.dataset.modalWired = '1';
    img.addEventListener('click', () => openImageModal(img.src, img.alt));
    img.style.cursor = img.style.cursor || 'pointer';
  });

  const modal = document.getElementById('imageModal');
  const content = modal ? modal.querySelector('.modal-content') : null;
  const closeBtn = modal ? modal.querySelector('.modal-close') : null;
  if (modal) {
    // Click outside content closes
    modal.addEventListener('click', () => closeImageModal());
  }
  if (content) {
    // Prevent modal background click when clicking content
    content.addEventListener('click', (e) => e.stopPropagation());
  }
  if (closeBtn) {
    closeBtn.addEventListener('click', () => closeImageModal());
  }

  // Close modal on Escape key
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      closeImageModal();
    }
  });
}

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
    const topics = Array.isArray(repo.topics) ? repo.topics : [];

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

    // Tags (topics)
    let tagsWrap = null;
    if (topics && topics.length) {
      tagsWrap = document.createElement('div');
      tagsWrap.className = 'tags';
      topics.slice(0, 6).forEach(t => {
        const tag = document.createElement('span');
        tag.className = 'tag';
        tag.textContent = String(t);
        tagsWrap.appendChild(tag);
      });
    }

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
    if (tagsWrap) card.appendChild(tagsWrap);
    card.appendChild(meta);
    fragment.appendChild(card);
  });

  container.appendChild(fragment);
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

function initCaptchaLoaders() {
  // Cloudflare Turnstile
  if (document.querySelector('.cf-turnstile') && !document.querySelector('script[src^="https://challenges.cloudflare.com/turnstile/"]')) {
    const s = document.createElement('script');
    s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
    s.async = true; s.defer = true;
    document.head.appendChild(s);
  }
  // hCaptcha
  if (document.querySelector('.h-captcha') && !document.querySelector('script[src^="https://js.hcaptcha.com/1/api.js"]')) {
    const s = document.createElement('script');
    s.src = 'https://js.hcaptcha.com/1/api.js';
    s.async = true; s.defer = true;
    document.head.appendChild(s);
  }
  // Google reCAPTCHA v3
  const rk = document.body && document.body.dataset ? document.body.dataset.recaptchaSiteKey : null;
  if (rk && !window.RECAPTCHA_SITE_KEY) {
    try { window.RECAPTCHA_SITE_KEY = rk; } catch (e) {}
  }
  if (rk && !document.querySelector('script[src^="https://www.google.com/recaptcha/api.js?"]')) {
    const s = document.createElement('script');
    s.src = 'https://www.google.com/recaptcha/api.js?render=' + encodeURIComponent(rk);
    s.async = true; s.defer = true;
    document.head.appendChild(s);
  }
}

function initContactForm() {
  const form = document.getElementById('contact-form');
  const submitBtn = document.getElementById('contact-submit');
  const statusEl = document.getElementById('contact-status');
  if (!form) return;

  function setStatus(msg, ok = true) {
    if (!statusEl) return;
    statusEl.textContent = msg || '';
    statusEl.style.color = ok ? 'var(--text-dim)' : '#ffb4b4';
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = 'Sending…';
    }
    setStatus('Sending your message…');

    const payload = {
      name: document.getElementById('name')?.value?.trim() || '',
      email: document.getElementById('email')?.value?.trim() || '',
      message: document.getElementById('message')?.value?.trim() || '',
      website: document.getElementById('website')?.value || ''
    };

    // Include CAPTCHA token if available (Turnstile / hCaptcha)
    const cfTokenEl = document.querySelector('input[name="cf-turnstile-response"]');
    if (cfTokenEl && cfTokenEl.value) {
      payload.cf_turnstile_token = cfTokenEl.value;
    }
    const hcTokenEl = document.querySelector('textarea[name="h-captcha-response"], input[name="h-captcha-response"]');
    if (hcTokenEl && hcTokenEl.value) {
      payload.hcaptcha_token = hcTokenEl.value;
    }

    // Google reCAPTCHA v3: if grecaptcha is available and a site key is provided, get a token
    if (window.grecaptcha && window.RECAPTCHA_SITE_KEY) {
      try {
        // Ensure API is ready before executing
        await new Promise((resolve) => {
          if (grecaptcha.execute) return resolve();
          grecaptcha.ready(resolve);
        });
        const token = await grecaptcha.execute(window.RECAPTCHA_SITE_KEY, { action: 'contact' });
        if (token) payload.recaptcha_token = token;
      } catch (e2) {
        // If token fetch fails, continue; server will reject if required
      }
    }

    try {
      const res = await fetch('/api/contact', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok || !json.ok) {
        const msg = (json && json.error === 'validation_error') ? 'Please fill in all fields correctly.' :
                    (json && json.error === 'rate_limited') ? 'Too many messages from your IP. Please try again later.' :
                    (json && (json.error === 'captcha_required' || json.error === 'captcha_failed')) ? 'Please complete the CAPTCHA verification.' :
                    'Something went wrong. Please try again.';
        setStatus(msg, false);
      } else {
        setStatus('Thanks! Your message has been sent.');
        form.reset();
      }
    } catch (err) {
      setStatus('Network error. Please try again later.', false);
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Send message';
      }
    }
    return false;
  });
}

function debounce(fn, wait) {
  let t = null;
  return function debounced(...args) {
    if (t) clearTimeout(t);
    t = setTimeout(() => fn.apply(this, args), wait);
  }
}

// Typewriter effect for CLI-esque titles (e.g., .section-title on repos page)
function initTypewriterTitles() {
  // Select all headings that use the CLI-esque monospace style, excluding the header brand title
  const selectors = '.section-title, .hero-title, .spotlight-title';
  const els = Array.from(document.querySelectorAll(selectors));
  if (!els.length) return;

  // Preserve original text and prepare elements
  els.forEach(el => {
    if (el.dataset.typeInit === '1') return; // avoid double init
    el.dataset.typeInit = '1';
    const full = (el.textContent || '').trim();
    el.setAttribute('aria-label', full);
    el.dataset.fullText = full;
    // Capture original computed white-space so we can restore it after typing
    try {
      const cs = window.getComputedStyle(el);
      el.dataset.ws = cs.whiteSpace || '';
      // Capture original height to avoid layout collapse while typing
      el.dataset.origH = String(el.offsetHeight || '');
    } catch (e) {
      // no-op
    }
    // We don't clear text now; we start typing only when visible
  });

  const BLINK_PERIOD = 1000; // ms
  const CARET_BLINK_LIFETIME_MS = 3000; // default lifetime for small titles like project spotlights
  const syncCaretBlink = (caretEl, removeAfterMs = null) => {
    if (!caretEl) return;
    const now = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
    const phase = now % BLINK_PERIOD;
    const delayToNextBoundary = (BLINK_PERIOD - phase) % BLINK_PERIOD;
    // Wait until the next global blink boundary, then start the animation aligned
    setTimeout(() => {
      const t = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
      const currentPhase = t % BLINK_PERIOD;
      // Use a negative animationDelay to align the animation phase precisely to the global clock
      try { caretEl.style.animationDelay = `${-currentPhase}ms`; } catch (e) { /* no-op */ }
      caretEl.classList.add('blink');
      // Optionally remove caret after some time (used for spotlight titles)
      if (typeof removeAfterMs === 'number' && isFinite(removeAfterMs) && removeAfterMs >= 0) {
        setTimeout(() => {
          try {
            // Remove the caret element entirely so it no longer affects layout or accessibility
            if (caretEl && caretEl.parentNode) {
              caretEl.parentNode.removeChild(caretEl);
            }
          } catch (e) { /* no-op */ }
        }, removeAfterMs);
      }
    }, delayToNextBoundary);
  };

  const typeElement = (el) => {
    if (!el || el.dataset.typed === '1') return;
    const full = el.dataset.fullText || '';
    // If empty or already typed, do nothing
    if (!full || full.length === 0) {
      el.dataset.typed = '1';
      return;
    }
    // Start from empty and type in
    // Preserve block height to prevent layout shift while typing
    const origH = el.dataset.origH ? parseFloat(el.dataset.origH) : 0;
    if (origH > 0) {
      el.style.minHeight = origH + 'px';
    }

    // Prepare dedicated text node and caret so we can update text without removing caret
    while (el.firstChild) el.removeChild(el.firstChild);
    const textNode = document.createTextNode('');
    const caret = document.createElement('span');
    caret.className = 'type-caret';
    caret.setAttribute('aria-hidden', 'true');
    el.appendChild(textNode);
    el.appendChild(caret);
    // Keep original white-space so wrapping occurs naturally during typing

    const minDelay = 20;  // ms per char
    const maxDelay = 60;  // vary a bit for a natural feel
    let i = 0;

    const step = () => {
      if (i < full.length) {
        textNode.nodeValue = full.slice(0, i + 1);
        i += 1;
        const jitter = Math.random() * (maxDelay - minDelay) + minDelay;
        setTimeout(step, jitter);
      } else {
        // Typing complete: mark done and remove temporary sizing only
        el.dataset.typed = '1';
        el.style.minHeight = '';
        // Start caret blinking after completion, synchronized to global timing
        // Only spotlight (project showcase) titles should lose their caret after a few seconds;
        // big titles like hero and section titles keep blinking indefinitely.
        // Additionally, hide carets for the "What you'll find here" and "Why it exists" section titles (in #about)
        const shouldRemoveCaret = (
          el.classList.contains('spotlight-title') ||
          (el.classList.contains('section-title') && el.closest('#about'))
        );
        const removeAfter = shouldRemoveCaret ? CARET_BLINK_LIFETIME_MS : null;
        syncCaretBlink(caret, removeAfter);
      }
    };
    step();
  };

  // Observe when elements enter the viewport
  const io = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      const el = entry.target;
      if (entry.isIntersecting && el.dataset.typed !== '1') {
        typeElement(el);
        io.unobserve(el);
      }
    });
  }, {
    root: null,
    threshold: 0.1
  });

  els.forEach(el => {
    // If already in view on load, start immediately; otherwise observe
    const rect = el.getBoundingClientRect();
    const inView = rect.top < (window.innerHeight || document.documentElement.clientHeight) && rect.bottom > 0;
    if (inView) {
      typeElement(el);
    } else {
      io.observe(el);
    }
  });
}


// Theme handling: toggle and persistence using data-theme on <html>
function applyTheme(theme) {
  try {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('theme', theme);
  } catch (e) {
    // no-op
  }
}

function prefersReducedMotion() {
  try {
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (e) {
    return false;
  }
}

function themeSplashTransition(event, nextTheme, options = {}) {
  const duration = options.duration || 600;
  const root = document.documentElement;

  // Reduced motion: switch instantly
  if (prefersReducedMotion()) {
    applyTheme(nextTheme);
    updateThemeToggleLabel(nextTheme);
    return;
  }

  // Compute click/touch coordinates; fall back to button center or viewport center
  let x, y;
  if (event) {
    // Pointer events (mouse)
    if (typeof event.clientX === 'number' && typeof event.clientY === 'number' && (event.clientX !== 0 || event.clientY !== 0)) {
      x = event.clientX; y = event.clientY;
    // Touch events
    } else if (event.touches && event.touches[0]) {
      x = event.touches[0].clientX; y = event.touches[0].clientY;
    } else if (event.changedTouches && event.changedTouches[0]) {
      x = event.changedTouches[0].clientX; y = event.changedTouches[0].clientY;
    }
    // Element center (e.g., keyboard activation)
    if ((typeof x !== 'number' || typeof y !== 'number') && event.currentTarget && typeof event.currentTarget.getBoundingClientRect === 'function') {
      const rect = event.currentTarget.getBoundingClientRect();
      x = rect.left + rect.width / 2;
      y = rect.top + rect.height / 2;
    }
  }
  if (typeof x !== 'number') x = window.innerWidth / 2;
  if (typeof y !== 'number') y = window.innerHeight / 2;

  // Radius large enough to cover the furthest corner
  const maxX = Math.max(x, window.innerWidth - x);
  const maxY = Math.max(y, window.innerHeight - y);
  const radius = Math.ceil(Math.hypot(maxX, maxY));

  const supportsVT = typeof document.startViewTransition === 'function';
  if (supportsVT) {
    // Set vars for animation and enable scoped animations
    root.style.setProperty('--vt-x', x + 'px');
    root.style.setProperty('--vt-y', y + 'px');
    root.style.setProperty('--vt-r', radius + 'px');

    const cleanup = () => {
      root.classList.remove('theme-vt-active');
      // Optionally clear vars
      // root.style.removeProperty('--vt-x'); root.style.removeProperty('--vt-y'); root.style.removeProperty('--vt-r');
    };

    const transition = document.startViewTransition(() => {
      applyTheme(nextTheme);
      updateThemeToggleLabel(nextTheme);
    });

    transition.ready.then(() => {
      root.classList.add('theme-vt-active');
    }).catch(() => {});

    transition.finished.finally(cleanup);
    return;
  }

  // Fallback path: solid color overlay splash
  const body = document.body;
  const overlay = document.createElement('div');
  overlay.className = 'theme-splash-overlay';
  const targetColor = nextTheme === 'dark' ? '#0a0516' : '#f7f7fb';
  overlay.style.setProperty('--splash-x', x + 'px');
  overlay.style.setProperty('--splash-y', y + 'px');
  overlay.style.setProperty('--splash-size', '0px');
  overlay.style.setProperty('--splash-color', targetColor);

  body.appendChild(overlay);

  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      overlay.style.setProperty('--splash-size', radius + 'px');
    });
  });

  const onDone = () => {
    overlay.removeEventListener('transitionend', onDone);
    applyTheme(nextTheme);
    updateThemeToggleLabel(nextTheme);
    requestAnimationFrame(() => {
      if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
    });
  };

  const timer = setTimeout(onDone, duration + 50);
  overlay.addEventListener('transitionend', () => {
    clearTimeout(timer);
    onDone();
  }, { once: true });
}

function getInitialTheme() {
  try {
    const stored = localStorage.getItem('theme');
    if (stored === 'light' || stored === 'dark') return stored;
  } catch (e) {}
  // fallback to prefers-color-scheme
  try {
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) return 'dark';
  } catch (e) {}
  return 'dark'; // default to dark to match current styling
}

function updateThemeToggleLabel(theme) {
  const btn = document.getElementById('theme-toggle');
  if (!btn) return;
  const next = theme === 'dark' ? 'Light mode' : 'Dark mode';
  // Keep icon markup intact; update only accessible label
  btn.setAttribute('aria-label', 'Switch to ' + next);
}

function initTheme() {
  const current = document.documentElement.getAttribute('data-theme') || getInitialTheme();
  applyTheme(current);
  updateThemeToggleLabel(current);
  const btn = document.getElementById('theme-toggle');
  if (btn && !btn.dataset.themeWired) {
    btn.dataset.themeWired = '1';
    btn.addEventListener('click', (e) => {
      const now = document.documentElement.getAttribute('data-theme') || 'dark';
      const next = now === 'dark' ? 'light' : 'dark';
      // Use splash transition from the click position
      themeSplashTransition(e, next, { duration: 600 });
    });
  }
}
