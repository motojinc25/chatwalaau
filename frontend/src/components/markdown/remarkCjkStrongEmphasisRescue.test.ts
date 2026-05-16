// Unit tests for the CJK strong / emphasis rescue plugin (PRP-0056).
//
// Runner: Node 22 built-in `node:test` with `--experimental-strip-types`
// so the .ts file can be executed directly without a separate test
// framework. No new npm dependencies.
//
// Invoke from the frontend/ directory:
//   node --experimental-strip-types --no-warnings --test \
//        src/components/markdown/remarkCjkStrongEmphasisRescue.test.ts

import {strict as assert} from 'node:assert'
import {test} from 'node:test'

import {remarkCjkStrongEmphasisRescue, rewriteText} from './remarkCjkStrongEmphasisRescue.ts'

// ----- Helpers -------------------------------------------------------

interface AnyNode {
  type: string
  value?: string
  children?: AnyNode[]
}

const text = (value: string): AnyNode => ({type: 'text', value})
const para = (...children: AnyNode[]): AnyNode => ({type: 'paragraph', children})
const root = (...children: AnyNode[]): AnyNode => ({type: 'root', children})

function deepEqual(actual: AnyNode, expected: AnyNode, label: string): void {
  assert.deepStrictEqual(actual, expected, label)
}

// ----- Test case 1: Korean closing-after-paren -----------------------

test('rescues Korean strong span ending in `)` followed by Hangul letter', () => {
  const tree = root(para(text('**오늘은 2026년 5월 15일(금)**입니다.')))
  remarkCjkStrongEmphasisRescue()(tree)
  const expected = root(
    para(
      {
        type: 'strong',
        children: [text('오늘은 2026년 5월 15일(금)')],
      },
      text('입니다.'),
    ),
  )
  deepEqual(tree, expected, 'Korean (금)** 입니다. should produce strong + text')
})

// ----- Test case 2: Japanese fullwidth-adjacent text -----------------

test('rescues Japanese strong span between CJK runs', () => {
  const tree = root(para(text('日本語**括弧(あり)**続き')))
  remarkCjkStrongEmphasisRescue()(tree)
  const expected = root(
    para(
      text('日本語'),
      {type: 'strong', children: [text('括弧(あり)')]},
      text('続き'),
    ),
  )
  deepEqual(tree, expected, 'Japanese (あり)** 続き should produce strong sandwiched in text')
})

// ----- Test case 3: ASCII end-paren + period -------------------------

test('rescues ASCII strong span ending in `)` followed by letter', () => {
  const tree = root(para(text('**bold(end)**after.')))
  remarkCjkStrongEmphasisRescue()(tree)
  const expected = root(
    para(
      {type: 'strong', children: [text('bold(end)')]},
      text('after.'),
    ),
  )
  deepEqual(tree, expected, 'ASCII (end)**after. should produce strong + text')
})

// ----- Test case 4: single-asterisk emphasis -------------------------

test('rescues single-asterisk emphasis with CJK suffix', () => {
  const tree = root(para(text('*기울임(끝)*뒤')))
  remarkCjkStrongEmphasisRescue()(tree)
  const expected = root(
    para(
      {type: 'emphasis', children: [text('기울임(끝)')]},
      text('뒤'),
    ),
  )
  deepEqual(tree, expected, 'Korean *기울임(끝)*뒤 should produce emphasis + text')
})

// ----- Test case 5: already-strong stays as one strong node -----------

test('does not double-rescue an already-parsed strong node', () => {
  // CommonMark already parsed **ok** -> strong{text('ok')}; the trailing
  // ' 続き' arrives as a sibling text node with no asterisks.
  const tree = root(
    para(
      {type: 'strong', children: [text('ok')]},
      text(' 続き'),
    ),
  )
  remarkCjkStrongEmphasisRescue()(tree)
  const expected = root(
    para(
      {type: 'strong', children: [text('ok')]},
      text(' 続き'),
    ),
  )
  deepEqual(tree, expected, 'Idempotent on already-strong input')
})

// ----- Test case 6: inline code preservation -------------------------

test('does not rewrite asterisks inside inlineCode', () => {
  // inlineCode in mdast carries `.value` (no children). The walker
  // never descends into it. Verify that the value is untouched.
  const tree = root(
    para(
      {type: 'inlineCode', value: '**not bold**'} as AnyNode,
      text(' followed'),
    ),
  )
  remarkCjkStrongEmphasisRescue()(tree)
  const expected = root(
    para(
      {type: 'inlineCode', value: '**not bold**'} as AnyNode,
      text(' followed'),
    ),
  )
  deepEqual(tree, expected, 'inlineCode value is untouched')
})

// ----- Test case 7: fenced code preservation -------------------------

test('does not rewrite asterisks inside fenced code blocks', () => {
  const tree = root({
    type: 'code',
    value: 'const x = "**foo**"',
  } as AnyNode)
  remarkCjkStrongEmphasisRescue()(tree)
  const expected = root({
    type: 'code',
    value: 'const x = "**foo**"',
  } as AnyNode)
  deepEqual(tree, expected, 'code (fenced) value is untouched')
})

// ----- Test case 8: link URL preservation ----------------------------

test('does not rewrite asterisks inside link URLs (URL is not a text child)', () => {
  // mdast link: url is an attribute, children are the label.
  const tree = root(
    para({
      type: 'link',
      url: 'https://x.example/**path**',
      children: [text('label')],
    } as AnyNode),
  )
  remarkCjkStrongEmphasisRescue()(tree)
  const expected = root(
    para({
      type: 'link',
      url: 'https://x.example/**path**',
      children: [text('label')],
    } as AnyNode),
  )
  deepEqual(tree, expected, 'link.url remains untouched')
})

// ----- Test case 9: multi-line negative ------------------------------

test('does not rescue across newlines', () => {
  // Regex `[^*\n]*?` forbids `\n` inside the inner content.
  const tree = root(para(text('**foo\nbar**')))
  remarkCjkStrongEmphasisRescue()(tree)
  const expected = root(para(text('**foo\nbar**')))
  deepEqual(tree, expected, 'newline inside ** ... ** prevents rescue')
})

// ----- Test case 10: nothing-to-rescue -------------------------------

test('is a no-op on text without any rescue patterns', () => {
  const tree = root(para(text('plain text without emphasis markers')))
  remarkCjkStrongEmphasisRescue()(tree)
  const expected = root(para(text('plain text without emphasis markers')))
  deepEqual(tree, expected, 'plain text remains as a single text node')
})

// ----- Bonus: idempotency under repeated invocation ------------------

test('is idempotent when applied twice', () => {
  const tree = root(para(text('**(끝)**입니다.')))
  const transform = remarkCjkStrongEmphasisRescue()
  transform(tree)
  const afterOnce = JSON.parse(JSON.stringify(tree))
  transform(tree)
  deepEqual(tree, afterOnce, 'second invocation produces the same tree')
})

// ----- Bonus: pure rewriteText() unit ---------------------------------

test('rewriteText returns the text segments and strong/em nodes in order', () => {
  const out = rewriteText('a **b(c)**d *e(f)*g')
  assert.deepStrictEqual(out, [
    {type: 'text', value: 'a '},
    {type: 'strong', children: [{type: 'text', value: 'b(c)'}]},
    {type: 'text', value: 'd '},
    {type: 'emphasis', children: [{type: 'text', value: 'e(f)'}]},
    {type: 'text', value: 'g'},
  ])
})

// ----- Bonus: strong-before-emphasis pass order ----------------------

test('strong pass runs before emphasis so boundary asterisks survive', () => {
  // `**a*b*c**` -- the strong boundaries must win; the inner `*b*`
  // becomes emphasis inside the strong's text only if a future
  // emphasis pass were to recurse into strong children. Today the
  // emphasis pass runs only on leftover text segments, so the strong
  // children stay as a single text node "a*b*c".
  const out = rewriteText('**a*b*c**')
  assert.deepStrictEqual(out, [
    {
      type: 'strong',
      children: [{type: 'text', value: 'a*b*c'}],
    },
  ])
})
