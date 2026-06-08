import(
    "./module.js"
);
import(/*comment*/"./module.js");

import /*comment*/ "./module.js";
import { lexerOnlyConst } from /*comment*/ "./module.js";

const re1 = /test"pattern/; import("./module.js");
const re2 = /[a-z//]/; import("./module.js");

const re3 = /test`pattern/;
import(`./module.js`);
