document.querySelectorAll('[data-runtime]').forEach(button=>button.addEventListener('click',()=>{document.querySelectorAll('[data-runtime]').forEach(tab=>tab.setAttribute('aria-pressed',String(tab===button)));document.querySelector('#runtime-code').textContent=`choobi auth ${button.dataset.runtime}\nchoobi install`;}));document.querySelectorAll('[data-copy]').forEach(button=>button.addEventListener('click',async()=>{const target=document.getElementById(button.dataset.copy);try{await navigator.clipboard.writeText(target.textContent);button.textContent='copied!';document.querySelector('#copy-status').textContent='commands copied to clipboard.';setTimeout(()=>button.textContent='copy',1800);}catch{document.querySelector('#copy-status').textContent='select the highlighted commands to copy.';const range=document.createRange();range.selectNodeContents(target);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);}}));const replay=document.querySelector('#replay');const lines=[...document.querySelector('#demo').children];let timer;replay.addEventListener('click',()=>{clearTimeout(timer);replay.disabled=true;lines.forEach(line=>line.style.visibility='hidden');let i=0;function show(){lines[i++].style.visibility='visible';if(i<lines.length)timer=setTimeout(show,matchMedia('(prefers-reduced-motion: reduce)').matches?0:280);else replay.disabled=false;}show();});

const tuiPanel = document.querySelector('#tui-panel');
const tuiInput = document.querySelector('#tui-input');
const tuiScreens = {
  about: { lead: 'a little documentation agent for your repository.', detail: 'you write the code. i’ll keep the notes so you never have to think about it.', label: 'read the rest ↓' },
  workflow: { lead: 'commit → read the diff → update the docs.', detail: 'a separate docs commit follows yours.\nnothing to document? nothing gets changed.', label: 'see the process ↓' },
  install: { lead: 'bring a choobi into your repository.', detail: 'Python 3.9+ · Git · Claude or Codex CLI\nset it up once. then carry on building.', label: 'open setup instructions ↓' }
};
function showTuiScreen(screen) {
  const content = tuiScreens[screen];
  document.querySelectorAll('[data-screen]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.screen === screen)));
  const lead = document.createElement('p');
  lead.textContent = '> ' + content.lead;
  const detail = document.createElement('p');
  detail.className = 'tui-muted';
  detail.style.whiteSpace = 'pre-line';
  detail.textContent = content.detail;
  const link = document.createElement('a');
  link.href = '#' + screen;
  link.textContent = content.label;
  tuiPanel.replaceChildren(lead, detail, link);
}
document.querySelectorAll('[data-screen]').forEach(button => button.addEventListener('click', () => showTuiScreen(button.dataset.screen)));
function tuiMessage(message, detail = '') {
  const line = document.createElement('p');
  line.textContent = message;
  const sub = document.createElement('p');
  sub.className = 'tui-muted';
  sub.textContent = detail;
  tuiPanel.replaceChildren(line, sub);
}
document.querySelector('#tui-form').addEventListener('submit', event => {
  event.preventDefault();
  const command = tuiInput.value.trim().toLowerCase();
  tuiInput.value = '';
  if (!command) return;
  const screens = { '1': 'about', about: 'about', readme: 'about', '2': 'workflow', process: 'workflow', '3': 'install', install: 'install' };
  if (screens[command]) showTuiScreen(screens[command]);
  else if (command === 'help' || command === '?') tuiMessage('commands: readme · process · install · hello · clear', 'explore here, or scroll down for the whole story.');
  else if (command === 'hello') { document.querySelector('#hello').textContent = 'hello, favourite human.'; tuiMessage('(^_^) hello, favourite human.', 'you make something good. i’ll keep the notes.'); }
  else if (command === 'clear') showTuiScreen('about');
  else tuiMessage('unknown command: ' + command, 'type help for a list of demo commands.');
});
