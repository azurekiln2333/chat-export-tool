/**
 * Vue entry point. The app is mounted on `#app` and all API/media requests use
 * paths relative to the document base, so it can be served behind a proxy
 * prefix without configuration.
 */
import { createApp } from 'vue';

import App from './components/App.vue';
import './styles.css';

createApp(App).mount('#app');
