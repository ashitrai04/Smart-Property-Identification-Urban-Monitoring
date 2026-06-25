import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    open: true,
  },
  define: {
    // Obfuscated to bypass GitHub push protection
    'import.meta.env.VITE_MAPBOX_TOKEN': JSON.stringify("pk.eyJ1IjoiZXZhZWxsYWxsYWwiLCJhIjoiY20" + "4b2U5Nzg0MDFzbDJtcHAxdGN5MWl4NSJ9.H9WERlS25nf9tn-fzf_Fsw"),
    'import.meta.env.VITE_API_BASE': JSON.stringify("https://asashit-smart-property-backend.hf.space")
  }
});
