import { mkdir, readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const root = dirname(fileURLToPath(import.meta.url));
const output = resolve(root, 'build');
await mkdir(output, { recursive: true });
await build({
  entryPoints: [resolve(root, 'public/bim-viewer.js')],
  outfile: resolve(output, 'bim-viewer.js'),
  bundle: true,
  format: 'esm',
  target: 'es2022',
  plugins: [{
    name: 'three-utility-extension',
    setup(builder) {
      builder.onResolve({ filter: /^three\/examples\/jsm\/utils\/BufferGeometryUtils$/ }, () => ({
        path: resolve(root, 'node_modules/three/examples/jsm/utils/BufferGeometryUtils.js'),
      }));
      builder.onLoad({ filter: /BufferGeometryUtils\.js$/ }, async (args) => ({
        contents: (await readFile(args.path, 'utf8')) + '\nexport const mergeGeometries = mergeBufferGeometries;\n',
        loader: 'js',
      }));
    },
  }],
});