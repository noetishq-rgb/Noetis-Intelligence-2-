import {readdirSync,readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
for(const file of readdirSync('web').filter(f=>f.endsWith('.js')))execFileSync(process.execPath,['--check','web/'+file],{stdio:'inherit'});
for(const file of ['index.html','app.js','brain.js','brain-api.js','styles.css'])if(!readFileSync('web/'+file).length)throw new Error('Missing frontend asset '+file);
console.log('Frontend assets and module syntax verified.');
