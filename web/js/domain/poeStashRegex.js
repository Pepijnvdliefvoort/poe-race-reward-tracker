/**
 * Path of Exile stash-style regex matching (approximation of in-game behavior).
 *
 * PoE uses case-insensitive multiline matching (~ JS flags `im`). With quotes,
 * space-separated `"…"` terms are ANDed; `|` outside quotes ORs branches.
 * Without quotes, whitespace splits AND terms; `foo|bar` (no spaces) is one
 * regex; spaced `a | b` is OR of branches (this app’s rule for disambiguation).
 *
 * A term may start with `!` to negate (must NOT match). Invalid regex for a
 * term falls back to case-insensitive substring matching for that term.
 *
 * This app matches only the item display name (one logical line), not full
 * Ctrl+C item text.
 */

const POE_REGEX_FLAGS = "im";

function quotesBalanced(s) {
    let inQuote = false;
    for (let i = 0; i < s.length; i++) {
        const c = s[i];
        if (c === "\\" && inQuote && i + 1 < s.length) {
            i++;
            continue;
        }
        if (c === '"') {
            inQuote = !inQuote;
        }
    }
    return !inQuote;
}

/**
 * Split on `|` only outside double quotes (PoE OR between quoted phrases).
 */
export function splitPoeOrBranches(query) {
    const branches = [];
    let start = 0;
    let i = 0;
    let inQuote = false;
    while (i < query.length) {
        const c = query[i];
        if (c === "\\" && inQuote && i + 1 < query.length) {
            i += 2;
            continue;
        }
        if (c === '"') {
            inQuote = !inQuote;
            i++;
            continue;
        }
        if (c === "|" && !inQuote) {
            branches.push(query.slice(start, i));
            start = i + 1;
        }
        i++;
    }
    branches.push(query.slice(start));
    return branches.map((b) => b.trim()).filter((b) => b.length > 0);
}

/**
 * Split one OR branch into AND terms: `"quoted"` chunks or unquoted non-whitespace runs.
 */
export function splitPoeAndTerms(branch) {
    const terms = [];
    const b = branch.trim();
    let i = 0;
    while (i < b.length) {
        while (i < b.length && /\s/.test(b[i])) {
            i++;
        }
        if (i >= b.length) {
            break;
        }
        if (b[i] === '"') {
            i++;
            let buf = "";
            while (i < b.length) {
                if (b[i] === "\\" && i + 1 < b.length) {
                    buf += b[i] + b[i + 1];
                    i += 2;
                    continue;
                }
                if (b[i] === '"') {
                    i++;
                    break;
                }
                buf += b[i++];
            }
            terms.push(buf);
        } else {
            const start = i;
            while (i < b.length && !/\s/.test(b[i])) {
                i++;
            }
            terms.push(b.slice(start, i));
        }
    }
    return terms;
}

function termMatches(termBody, haystack) {
    let neg = false;
    let body = termBody;
    if (body.startsWith("!")) {
        neg = true;
        body = body.slice(1);
    }
    try {
        const re = new RegExp(body, POE_REGEX_FLAGS);
        const ok = re.test(haystack);
        return neg ? !ok : ok;
    } catch {
        const lit = body.toLowerCase();
        const h = haystack.toLowerCase();
        const ok = h.includes(lit);
        return neg ? !ok : ok;
    }
}

/**
 * Match haystack against a PoE-style stash regex query.
 */
export function poeStashRegexMatches(haystack, rawQuery) {
    const q = rawQuery.trim();
    if (!q) {
        return true;
    }

    if (!quotesBalanced(q)) {
        return haystack.toLowerCase().includes(q.toLowerCase());
    }

    if (!q.includes('"')) {
        // Spaced ` | ` acts like PoE OR between branches; unbroken `foo|bar` is one regex (alternation).
        if (/\s\|\s/.test(q)) {
            const branches = splitPoeOrBranches(q);
            if (branches.length === 0) {
                return false;
            }
            return branches.some((branch) => {
                const terms = splitPoeAndTerms(branch);
                if (terms.length === 0) {
                    return false;
                }
                return terms.every((t) => termMatches(t, haystack));
            });
        }
        const terms = splitPoeAndTerms(q);
        if (terms.length === 0) {
            return false;
        }
        return terms.every((t) => termMatches(t, haystack));
    }

    const branches = splitPoeOrBranches(q);
    if (branches.length === 0) {
        return false;
    }

    return branches.some((branch) => {
        const terms = splitPoeAndTerms(branch);
        if (terms.length === 0) {
            return false;
        }
        return terms.every((t) => termMatches(t, haystack));
    });
}
