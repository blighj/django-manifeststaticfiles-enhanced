export const moduleConst = "module";
// Static imports.
import rootConst from "/static/absolute_root.js";
import testConst from "./module_test.js";
import * as NewModule from "./module_test.js";
import*as m from "./module_test.js";
import *as m from "./module_test.js";
import* as m from "./module_test.js";
import*  as  m from "./module_test.js";
import { testConst as alias } from "./module_test.js";
import { firstConst, secondConst } from "./module_test.js";
import {
    firstVar1 as firstVarAlias,
    $second_var_2 as secondVarAlias
} from "./module_test.js";
import relativeModule from "../nested/js/nested.js";

// Dynamic imports with import attributes (second argument).
const dynamicModule = import("./module_test.js");
const dynamicModule = import('./module_test.js');
const dynamicModule = import(`./module_test.js`);
const dynamicModule = import("./module_test.js", { with: { type: "json" } });

// import using the with attribute
import k from"./other.css"with{type:"css"};

import*as l from "/static/absolute_root.js";
import*as h from "/static/absolute_root.js";
import*as m from "/static/absolute_root.js";
import {BaseComponent as g} from "/static/absolute_root.js";


// Modules exports to aggregate modules.
export * from "./module_test.js";
export { testConst } from "./module_test.js";
export {
    firstVar as firstVarAlias,
    secondVar as secondVarAlias
} from "./module_test.js";


// Mid-line import (after other code on the same line, as seen in minified output).
var placeholder; import { testConst as alias2 } from "./module_test.js";
// Multiple imports on one minified line.
import{testConst as alias3}from"./module_test.js";import relativeModule2 from"../nested/js/nested.js";
// ASI: no trailing semicolon.
import{testConst as alias4}from"./module_test.js"

// ASI: spaced form without trailing semicolon.
import * as mAsi from "./module_test.js"
import { testConst as aliasAsi } from "./module_test.js"

// Imports inside string literals should be ignored.
const msgStr = 'import { foo } from "./module_test_missing.js";';
const helpStr = "import { bar } from './module_test_missing.js';";
const tmplLit = `import { baz } from "./module_test_missing.js";`;
const dynStr = 'const x = import("./module_test_missing.js");';
const multiLineLit = `
import { baz } from "./module_test_missing.js";
`;

// Export without from must not consume a subsequent import's from clause.
export { testConst };
import { firstConst } from "./module_test.js";

// Imports in JSDoc block comments after a real import (regression: cross-boundary match).
import '../nested/js/nested.js';
/**
 * @example
 * import { something } from "./module_test_missing.js";
 */
function jsdocExample() {}

// These should not be processed
// @returns {import("./non-existent-1").something}
/* @returns {import("./non-existent-2").something} */
'import("./non-existent-3")'
"import('./non-existent-4')"
`import("./non-existent-5")`
r = /import/;
/**
 * @param {HTMLElement} elt
 * @returns {import("./htmx").HtmxTriggerSpecification[]}
 */
