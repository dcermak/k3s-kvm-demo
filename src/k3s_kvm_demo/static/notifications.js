(() => {
  const dismissed = new Set();

  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-dismiss-notification]');
    if (!button) return;
    const notification = button.closest('[data-notification]');
    if (notification.dataset.notificationId) {
      dismissed.add(notification.dataset.notificationId);
    }
    notification.remove();
  });

  // Polls, including responses already in flight, must not restore dismissed bubbles.
  document.addEventListener('htmx:afterSwap', () => {
    document.querySelectorAll('[data-notification-id]').forEach((notification) => {
      if (dismissed.has(notification.dataset.notificationId)) notification.remove();
    });
  });
})();
