/* =============================================
   STOREFRONTBOT — script.js
   ============================================= */

const silk = [0.25, 0.46, 0.45, 0.94];

/* ── NAV scroll behavior ── */
(function () {
  let lastY = 0;
  const nav = document.querySelector('nav');
  window.addEventListener('scroll', () => {
    const y = window.scrollY;
    nav.classList.toggle('scrolled', y > 20);
    nav.classList.toggle('hidden', y > lastY + 40 && y > 200);
    if (y < lastY || y < 80) nav.classList.remove('hidden');
    lastY = y;
  }, { passive: true });
})();

/* ── Mobile menu ── */
const hamburger = document.querySelector('.hamburger');
const mobileMenu = document.querySelector('.mobile-menu');
hamburger?.addEventListener('click', () => {
  mobileMenu.classList.toggle('open');
});
mobileMenu?.querySelectorAll('a').forEach(a => {
  a.addEventListener('click', () => mobileMenu.classList.remove('open'));
});

/* ── Reveal on scroll ── */
const reveals = document.querySelectorAll('.reveal');
const observer = new IntersectionObserver((entries) => {
  entries.forEach(e => {
    if (e.isIntersecting) {
      e.target.classList.add('visible');
    }
  });
}, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
reveals.forEach(el => observer.observe(el));

/* ── Counter animation ── */
function animateCounter(el, target, suffix = '') {
  let start = 0;
  const duration = 1400;
  const step = timestamp => {
    if (!start) start = timestamp;
    const progress = Math.min((timestamp - start) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3);
    el.textContent = Math.floor(eased * target).toLocaleString() + suffix;
    if (progress < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

const statsObserver = new IntersectionObserver((entries) => {
  entries.forEach(e => {
    if (e.isIntersecting) {
      const counters = e.target.querySelectorAll('[data-count]');
      counters.forEach(c => {
        const target = parseInt(c.dataset.count);
        const suffix = c.dataset.suffix || '';
        animateCounter(c, target, suffix);
      });
      statsObserver.unobserve(e.target);
    }
  });
}, { threshold: 0.3 });
const statsGrid = document.querySelector('.stats-grid');
if (statsGrid) statsObserver.observe(statsGrid);

/* ── Features preview swap ── */
const featureCards = document.querySelectorAll('.feature-card');
const previewPanels = document.querySelectorAll('.preview-panel');

featureCards.forEach((card, i) => {
  card.addEventListener('click', () => {
    featureCards.forEach(c => c.classList.remove('active'));
    previewPanels.forEach(p => p.classList.remove('active'));
    card.classList.add('active');
    if (previewPanels[i]) previewPanels[i].classList.add('active');
  });
});
// activate first on load
if (featureCards[0]) featureCards[0].classList.add('active');
if (previewPanels[0]) previewPanels[0].classList.add('active');

/* ── Period buttons (chart) ── */
const periodBtns = document.querySelectorAll('.period-btn');
const chartBars = document.querySelectorAll('.bar');

const datasets = {
  '7D': [40, 55, 45, 70, 60, 85, 75],
  '1M': [30, 60, 45, 80, 55, 70, 90, 65, 75, 85, 60, 95],
  '3M': [50, 65, 80, 55, 90, 70, 85],
  '1Y': [45, 60, 75, 55, 85, 70, 95],
};

function renderBars(data) {
  const max = Math.max(...data);
  const bars = document.querySelectorAll('.bar');
  bars.forEach((bar, i) => {
    const pct = data[i % data.length] / max * 100;
    bar.style.height = pct + '%';
    bar.classList.toggle('active', i === data.indexOf(max));
  });
}

periodBtns.forEach(btn => {
  btn.addEventListener('click', () => {
    periodBtns.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderBars(datasets[btn.dataset.period] || datasets['7D']);
  });
});
renderBars(datasets['7D']);

/* ── Billing toggle ── */
const toggleSwitch = document.querySelector('.toggle-switch');
const monthlyPrices = document.querySelectorAll('.price-monthly');
const annualPrices = document.querySelectorAll('.price-annual');
const annualLabel = document.querySelector('.label-annual');
const monthlyLabel = document.querySelector('.label-monthly');
let isAnnual = false;

toggleSwitch?.addEventListener('click', () => {
  isAnnual = !isAnnual;
  toggleSwitch.classList.toggle('on', isAnnual);
  monthlyPrices.forEach(el => el.style.display = isAnnual ? 'none' : '');
  annualPrices.forEach(el => el.style.display = isAnnual ? '' : 'none');
  if (annualLabel) annualLabel.classList.toggle('active', isAnnual);
  if (monthlyLabel) monthlyLabel.classList.toggle('active', !isAnnual);
});
// hide annual prices initially
if (annualPrices) annualPrices.forEach(el => el.style.display = 'none');

/* ── Testimonials pause ── */
const testimonialTrack = document.querySelector('.testimonials-track');
const pauseBtn = document.querySelector('.pause-btn');
let isPaused = false;

pauseBtn?.addEventListener('click', () => {
  isPaused = !isPaused;
  testimonialTrack?.classList.toggle('paused', isPaused);
  pauseBtn.textContent = isPaused ? '▶ Play' : '⏸ Pause';
});

/* ── 3D phone tilt on mouse move ── */
const phoneMockup = document.querySelector('.phone-mockup');
if (phoneMockup) {
  document.addEventListener('mousemove', (e) => {
    const cx = window.innerWidth / 2;
    const cy = window.innerHeight / 2;
    const dx = (e.clientX - cx) / cx;
    const dy = (e.clientY - cy) / cy;
    const rotY = dx * 14;
    const rotX = -dy * 10;
    phoneMockup.style.transform = `rotateY(${rotY}deg) rotateX(${rotX}deg)`;
  });
  document.addEventListener('mouseleave', () => {
    phoneMockup.style.transform = 'rotateY(0deg) rotateX(0deg)';
  });
}

/* ── FAQ accordion ── */
const faqItems = document.querySelectorAll('.faq-item');
faqItems.forEach(item => {
  item.querySelector('.faq-q').addEventListener('click', () => {
    const isOpen = item.classList.contains('open');
    faqItems.forEach(i => i.classList.remove('open'));
    if (!isOpen) item.classList.add('open');
  });
});

const expandAllBtn = document.querySelector('#expand-all');
const collapseAllBtn = document.querySelector('#collapse-all');
expandAllBtn?.addEventListener('click', () => faqItems.forEach(i => i.classList.add('open')));
collapseAllBtn?.addEventListener('click', () => faqItems.forEach(i => i.classList.remove('open')));