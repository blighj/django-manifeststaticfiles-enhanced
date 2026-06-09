#!/usr/bin/env node
/**
 * Acorn-based ground truth extractor for JS import/export statements.
 *
 * Reads absolute file paths from stdin (one per line).
 * Emits one JSON line per file: { path, ok, imports }
 *
 * Each import entry:
 *   { kind, url, start, end }
 *
 * Kinds:
 *   import_from              — import … from '…'  /  export … from '…'
 *   dynamic_import           — import('literal')
 *   dynamic_import_nonliteral — import(expression)
 */

'use strict';

const fs       = require('fs');
const readline = require('readline');

let acorn, walk;
try {
  acorn = require('acorn');
  walk  = require('acorn-walk');
} catch (e) {
  process.stderr.write(`ERROR: ${e.message}\nRun: npm install  (in the scripts/ directory)\n`);
  process.exit(1);
}

const BASE_OPTS = { ecmaVersion: 2024, locations: false };

function parse(source) {
  try {
    return acorn.parse(source, { ...BASE_OPTS, sourceType: 'module' });
  } catch (_) {
    return acorn.parse(source, { ...BASE_OPTS, sourceType: 'script' });
  }
}

function isBareSpecifier(url) {
  if (!url) return false;
  if (url.startsWith('./') || url.startsWith('../') || url.startsWith('/')) return false;
  if (/^[a-z][a-z0-9+.-]*:/i.test(url)) return false;  // absolute URL (https://, data:, etc.)
  return true;
}

function extractImports(filePath, source) {
  let ast;
  try {
    ast = parse(source);
  } catch (e) {
    return { path: filePath, ok: false, error: e.message, imports: [] };
  }

  const imports = [];

  walk.simple(ast, {
    ImportDeclaration(node) {
      imports.push({
        kind: 'import_from',
        url:   node.source.value,
        start: node.start,
        end:   node.end,
      });
    },
    ExportNamedDeclaration(node) {
      if (node.source) {
        imports.push({
          kind: 'import_from',
          url:   node.source.value,
          start: node.start,
          end:   node.end,
        });
      }
    },
    ExportAllDeclaration(node) {
      imports.push({
        kind: 'import_from',
        url:   node.source.value,
        start: node.start,
        end:   node.end,
      });
    },
    ImportExpression(node) {
      const isLiteral = node.source.type === 'Literal';
      imports.push({
        kind:  isLiteral ? 'dynamic_import' : 'dynamic_import_nonliteral',
        url:   isLiteral ? node.source.value : '',
        start: node.start,
        end:   node.end,
      });
    },
  });

  // A bare module specifier (e.g. "react", "jquery") is only valid in a browser
  // with an import map. One bare specifier proves the whole file is a build
  // artifact that browsers cannot execute directly — treat it as having no
  // actionable imports.
  const hasBareSpecifier = imports.some(
    imp => imp.kind !== 'dynamic_import_nonliteral' && isBareSpecifier(imp.url)
  );
  if (hasBareSpecifier) {
    return { path: filePath, ok: true, imports: [], build_artifact: true };
  }

  return { path: filePath, ok: true, imports };
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

rl.on('line', (line) => {
  line = line.trim();
  if (!line) return;

  let source;
  try {
    source = fs.readFileSync(line, 'utf8');
  } catch (e) {
    process.stdout.write(
      JSON.stringify({ path: line, ok: false, error: e.message, imports: [] }) + '\n'
    );
    return;
  }

  process.stdout.write(JSON.stringify(extractImports(line, source)) + '\n');
});
