import { createApp } from 'vue'
import App from './App.vue'
import './style.css'
import './archive.css'
import './creative.css'
import './display-fonts.css'
import './artist-palettes.css'
import './production.css'
import './living-colors.css'
import './spatial.css'
import './atelier.css'
const raw = document.getElementById('studio-data').textContent
createApp(App, { initial: raw.startsWith('__STUDIO') ? {assets:[],tasks:[]} : JSON.parse(raw) }).mount('#app')
