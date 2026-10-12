import base from '/home/frontend/apps/conversations/vite.config.mts';

// Keep auth, cookies and streaming on the browser's origin through port forwarding.
// Preserve Host so Django's same-origin CSRF check also sees the forwarded port.
export default {
  ...base,
  server: {
    ...base.server,
    proxy: {
      '/api/': { target: 'http://conversations:8000', changeOrigin: false },
      '/admin/': { target: 'http://conversations:8000', changeOrigin: false },
      '/static/': { target: 'http://conversations:8000', changeOrigin: false },
    },
  },
};
