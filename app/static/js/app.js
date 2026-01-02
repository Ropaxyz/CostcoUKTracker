/**
 * Costco UK Stock Tracker - Client-side JavaScript
 * Minimal JS - most interactivity via HTMX
 */

// Show loading indicator on HTMX requests
document.body.addEventListener('htmx:beforeRequest', function(evt) {
    document.body.classList.add('htmx-loading');
});

document.body.addEventListener('htmx:afterRequest', function(evt) {
    document.body.classList.remove('htmx-loading');
});

// Handle HTMX errors
document.body.addEventListener('htmx:responseError', function(evt) {
    console.error('HTMX request failed:', evt.detail);
    alert('Request failed. Please try again.');
});

// Auto-dismiss alerts after 5 seconds
document.addEventListener('DOMContentLoaded', function() {
    const alerts = document.querySelectorAll('.alert-dismissible');
    alerts.forEach(function(alert) {
        setTimeout(function() {
            alert.style.opacity = '0';
            setTimeout(function() {
                alert.remove();
            }, 300);
        }, 5000);
    });
});

// Confirm before dangerous actions
document.body.addEventListener('htmx:confirm', function(evt) {
    if (evt.detail.elt.hasAttribute('hx-confirm')) {
        evt.preventDefault();
        if (confirm(evt.detail.elt.getAttribute('hx-confirm'))) {
            evt.detail.issueRequest();
        }
    }
});

// Format relative times
function formatRelativeTime(date) {
    const now = new Date();
    const diff = now - new Date(date);
    const minutes = Math.floor(diff / 60000);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);

    if (minutes < 1) return 'just now';
    if (minutes < 60) return minutes + 'm ago';
    if (hours < 24) return hours + 'h ago';
    return days + 'd ago';
}

// Update relative times every minute
function updateRelativeTimes() {
    document.querySelectorAll('[data-relative-time]').forEach(function(el) {
        const timestamp = el.getAttribute('data-relative-time');
        if (timestamp) {
            el.textContent = formatRelativeTime(timestamp);
        }
    });
}

setInterval(updateRelativeTimes, 60000);

// Copy to clipboard utility
function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(function() {
        // Show feedback
        const toast = document.createElement('div');
        toast.className = 'toast';
        toast.textContent = 'Copied!';
        document.body.appendChild(toast);
        setTimeout(function() {
            toast.remove();
        }, 2000);
    });
}
