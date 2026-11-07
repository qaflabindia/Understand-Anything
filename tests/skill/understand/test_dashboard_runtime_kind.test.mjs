import { afterEach, describe, expect, it } from 'vitest';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { dashboardRuntimeKind } from '../../../understand-anything-plugin/skills/understand-dashboard/runtime-kind.mjs';
const roots=[];
function fixture(tracked=false) {
 const root=mkdtempSync(join(tmpdir(),'ua-dashboard-runtime-')); roots.push(root);
 const plugin=join(root,'plugin'); mkdirSync(join(plugin,'packages/dashboard'),{recursive:true});
 writeFileSync(join(plugin,'packages/dashboard/vite.config.ts'),'export default {};');
 if(tracked) { execFileSync('git',['init'],{cwd:root,stdio:'ignore'}); execFileSync('git',['add','plugin/packages/dashboard/vite.config.ts'],{cwd:root}); }
 return {root,plugin};
}
afterEach(()=>{for(const root of roots.splice(0))rmSync(root,{recursive:true,force:true});});
describe('dashboard runtime source selection',()=>{
 it('keeps tracked local and fork sources active regardless of package version',()=>{const {plugin}=fixture(true);expect(dashboardRuntimeKind(plugin)).toBe('checkout');});
 it('uses the installed release path outside a tracked checkout',()=>{const {plugin}=fixture();expect(dashboardRuntimeKind(plugin)).toBe('installed');});
 it('does not mistake an unrelated ancestor git repository for tracked plugin source',()=>{const {root,plugin}=fixture();execFileSync('git',['init'],{cwd:root,stdio:'ignore'});expect(dashboardRuntimeKind(plugin)).toBe('installed');});
});
