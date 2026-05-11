/* ===================================================
   QRTrack – main.js
   Global utilities used across all pages
   =================================================== */

/**
 * Show a toast notification at the bottom-right.
 * @param {string} message
 * @param {string} [type='success']  'success' | 'error'
 */
function showToast(message, type = 'success') {
  // Create container if absent
  let container = document.getElementById('toastContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toastContainer';
    container.className = 'toast-container position-fixed bottom-0 end-0 p-3';
    document.body.appendChild(container);
  }

  const icon = type === 'error'
    ? '<i class="bi bi-x-circle-fill text-danger me-2"></i>'
    : '<i class="bi bi-check-circle-fill text-success me-2"></i>';

  const toastEl = document.createElement('div');
  toastEl.className = 'toast toast-custom align-items-center border-0 show';
  toastEl.setAttribute('role', 'alert');
  toastEl.innerHTML = `
    <div class="d-flex align-items-center p-3 gap-1">
      ${icon}
      <span class="me-auto">${message}</span>
      <button type="button" class="btn-close btn-close-white ms-2" data-bs-dismiss="toast"></button>
    </div>`;

  container.appendChild(toastEl);

  // Auto-remove after 3 s
  setTimeout(() => {
    toastEl.classList.remove('show');
    setTimeout(() => toastEl.remove(), 300);
  }, 3000);
}

/**
 * Copy text to clipboard and show toast.
 * @param {string} text
 * @param {string} [label]
 */
function copyToClipboard(text, label = 'คัดลอกสำเร็จ!') {
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text)
      .then(() => showToast(label))
      .catch(() => showToast('ไม่สามารถคัดลอกได้', 'error'));
  } else {
    // Fallback for older browsers
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      showToast(label);
    } catch {
      showToast('ไม่สามารถคัดลอกได้', 'error');
    }
    document.body.removeChild(ta);
  }
}
