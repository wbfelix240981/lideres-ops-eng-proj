// build.js — reconstrói index.html a partir de template.html + dados JSON de cada líder
const fs = require('fs');
const path = require('path');

const DIR = __dirname;

const template = fs.readFileSync(path.join(DIR, 'template.html'), 'utf-8');

const files = {
  WAGNER_METAS: 'wagner_metas.json',
  WAGNER_TASKS: 'wagner_tasks.json',
  BRUNO_TASKS: 'bruno_tasks.json',
  LEONARDO_TASKS: 'leonardo_tasks.json',
  JOAO_TASKS: 'joao_tasks.json',
  GUSTAVO_TASKS: 'gustavo_tasks.json',
  RODNEY_TASKS: 'rodney_tasks.json',
};

let out = template;
for (const [varName, fname] of Object.entries(files)) {
  const data = JSON.parse(fs.readFileSync(path.join(DIR, fname), 'utf-8'));
  out = out.replace(
    `const ${varName} = /*__${varName}__*/;`,
    `const ${varName} = ` + JSON.stringify(data, null, 1)
  );
}

let lastSync = { last_sync_utc: null };
try {
  lastSync = JSON.parse(fs.readFileSync(path.join(DIR, 'last_sync.json'), 'utf-8'));
} catch (e) {
  console.warn('last_sync.json não encontrado, seguindo sem horário de sincronização.');
}
out = out.replace(
  'const LAST_SYNC_UTC = /*__LAST_SYNC_UTC__*/;',
  'const LAST_SYNC_UTC = ' + JSON.stringify(lastSync.last_sync_utc) + ';'
);

const outPath = path.join(DIR, '..', 'publish_out', 'index.html');
fs.mkdirSync(path.dirname(outPath), { recursive: true });
fs.writeFileSync(outPath, out, 'utf-8');
console.log('Build concluído:', outPath, '(' + out.length + ' bytes)');
