import test from 'node:test';
import assert from 'node:assert/strict';
import { APP_PATHS, appLocation, createAppNavigation } from './app-navigation.js';

// A small history adapter tests URL policy; document identity and mounted React
// state are verified separately in the actual browser fixture.
function browserAt(href = 'https://studio.example/') {
  const browser = new EventTarget(),
    document = new EventTarget(),
    entries = [new URL(href).href];
  let index = 0;
  Object.assign(browser, {
    location: new URL(href),
    document,
    scrollTo() {},
    PopStateEvent: class extends Event {
      constructor(type, options) {
        super(type);
        this.state = options.state;
      }
    },
  });
  browser.history = {
    state: null,
    pushState(state, _title, url) {
      this.state = state;
      entries.splice(++index);
      entries.push(new URL(url, browser.location).href);
      browser.location = new URL(entries[index]);
    },
    replaceState(state, _title, url) {
      this.state = state;
      entries[index] = new URL(url, browser.location).href;
      browser.location = new URL(entries[index]);
    },
    back() {
      if (index) {
        browser.location = new URL(entries[--index]);
        browser.dispatchEvent(new Event('popstate'));
      }
    },
    forward() {
      if (index + 1 < entries.length) {
        browser.location = new URL(entries[++index]);
        browser.dispatchEvent(new Event('popstate'));
      }
    },
  };
  return browser;
}
function click(href, overrides = {}) {
  const attrs = { href },
    anchor = {
      getAttribute: (key) => attrs[key] ?? null,
      hasAttribute: (key) => Object.hasOwn(attrs, key),
      relList: { contains: () => false },
    };
  return {
    button: 0,
    target: { closest: () => anchor },
    preventDefault() {
      this.defaultPrevented = true;
    },
    ...overrides,
  };
}

test('real paths and public project/session/shot queries survive history and refresh parsing', () => {
  const browser = browserAt('https://studio.example/?project=story&entity=shot-2'),
    navigation = createAppNavigation(browser),
    seen = [];
  const release = navigation.subscribe(() => seen.push(navigation.getSnapshot()));
  assert.deepEqual(appLocation(browser.location.href), {
    pathname: '/',
    path: '/',
    search: '?project=story&entity=shot-2',
    hash: '',
    href: '/?project=story&entity=shot-2',
  });
  assert.equal(
    navigation.navigate('/quick-chat?session=conversation&submission=result', { scroll: false }),
    true,
  );
  navigation.navigate('/library', { scroll: false });
  browser.history.back();
  assert.equal(navigation.getSnapshot(), '/quick-chat?session=conversation&submission=result');
  browser.history.back();
  assert.equal(navigation.getSnapshot(), '/?project=story&entity=shot-2');
  browser.history.forward();
  assert.equal(
    appLocation(browser.location.href).search,
    '?session=conversation&submission=result',
  );
  assert.deepEqual(seen, [
    '/quick-chat?session=conversation&submission=result',
    '/library',
    '/quick-chat?session=conversation&submission=result',
    '/?project=story&entity=shot-2',
    '/quick-chat?session=conversation&submission=result',
  ]);
  release();
});

test('bare Quick Create links resume the latest replaceState query, while Works has a clean URL', () => {
  const browser = browserAt(),
    navigation = createAppNavigation(browser),
    release = navigation.install();
  navigation.navigate('/quick-chat?session=first', { scroll: false });
  browser.history.replaceState(null, '', '/quick-chat?session=latest&card=chosen');
  navigation.navigate('/settings', { scroll: false });
  navigation.navigate('/quick-chat', { scroll: false });
  assert.equal(navigation.getSnapshot(), '/quick-chat?session=latest&card=chosen');
  navigation.navigate('/?project=story&entity=shot', { scroll: false });
  navigation.navigate('/', { scroll: false });
  assert.equal(navigation.getSnapshot(), '/');
  navigation.navigate('/quick-chat', { restore: false, scroll: false });
  assert.equal(navigation.getSnapshot(), '/quick-chat');
  release();
});

test('normal same-origin app anchors are intercepted; native link exceptions stay native', () => {
  const browser = browserAt(),
    navigation = createAppNavigation(browser),
    release = navigation.install();
  for (const path of APP_PATHS) {
    const event = click(path);
    assert.equal(navigation.intercept(event), true);
    assert.equal(event.defaultPrevented, true);
  }
  const exceptions = [
    click('/library', { defaultPrevented: true }),
    ...[1, 2].map((button) => click('/library', { button })),
    ...['ctrlKey', 'metaKey', 'shiftKey', 'altKey'].map((key) =>
      click('/library', { [key]: true }),
    ),
    click('#workspace'),
    click('/freestyle#heading'),
    click('https://other.example/library'),
    click('/v1/assets/file/content'),
    click('/for-agents/skill.zip'),
  ];
  for (const event of exceptions) {
    const before = navigation.getSnapshot();
    assert.equal(navigation.intercept(event), false);
    assert.equal(navigation.getSnapshot(), before);
  }
  for (const attributes of [{ download: '' }, { target: '_blank' }, { rel: 'external' }]) {
    const event = click('/library');
    const anchor = event.target.closest();
    anchor.hasAttribute = (key) => Object.hasOwn(attributes, key);
    anchor.getAttribute = (key) => attributes[key] ?? (key === 'href' ? '/library' : null);
    anchor.relList = { contains: (value) => attributes.rel === value };
    assert.equal(navigation.intercept(event), false);
  }
  release();
});

test('history hooks share ownership and restore original methods on final cleanup', () => {
  const browser = browserAt(),
    navigation = createAppNavigation(browser),
    original = browser.history.pushState;
  const release = navigation.install(),
    unsubscribe = navigation.subscribe(() => {});
  assert.notEqual(browser.history.pushState, original);
  release();
  assert.notEqual(browser.history.pushState, original);
  unsubscribe();
  assert.equal(browser.history.pushState, original);
  assert.equal(navigation.navigate('https://external.example/library'), false);
  assert.equal(navigation.navigate('/unknown'), false);
});
