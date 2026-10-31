// Local stand-in for the story-chat backend (#422) so the experience can be judged before the
// server exists. It emits the same events the server will: nothing here calls the network,
// spends credits or creates a project.

const copy = {
  en: {
    title: 'First Contact in the Cornfield',
    zib: [
      'Zib',
      'A small green alien with big black eyes and a silver suit; curious, a little scared.',
    ],
    mia: ['Mia', 'An eight-year-old girl in a red raincoat and wellies; brave, holds a torch.'],
    joe: [
      'Grandpa Joe',
      'Mia’s grandfather, flannel shirt, slow and kind; does not believe her at first.',
    ],
    field: [
      'Cornfield at night',
      'Tall corn, a full moon, mist low on the ground, a farmhouse light far away.',
    ],
    pod: ['Crashed pod', 'A small round ship half buried in the soil, humming and glowing teal.'],
    shots: [
      ['Wide: a streak of teal light falls into the cornfield.', 'field', 'pod'],
      ['Mia at her bedroom window sees the glow, grabs her torch.', 'mia'],
      ['Mia pushes through the corn; the hum gets louder.', 'mia', 'field'],
      ['Close-up: the pod opens; two big black eyes blink in the dark.', 'zib', 'pod'],
      ['Mia slowly holds out her hand; Zib copies her.', 'mia', 'zib'],
      ['Their fingertips touch and every corn stalk lights up teal.', 'mia', 'zib', 'field'],
    ],
    hello:
      'Love it. I’ve made the first cards: two characters, the place and the ship. How long should the film be, and what mood — funny, tender or spooky?',
    helloChips: ['30 seconds, tender', 'Make it funny', 'Add a character'],
    shotsMade:
      'Here are 6 shots, 5 seconds each (30 s). Click any card to change it. Shall I draw Zib, Mia and the cornfield first, so every shot looks consistent?',
    shotsChips: ['Draw the characters', 'Change shot 3', 'Generate all shots'],
    drawQuote: 'Draw pictures of 2 characters, the place and the ship',
    drawn:
      'Done — have a look at the cards. If a face looks wrong, click it and tell me what to change. Ready to make the shots?',
    drawnChips: ['Generate all shots', 'Redraw Zib'],
    genQuote: 'Generate 6 shots, Standard quality',
    generating:
      'Generating your shots — each one appears on its card as it finishes. You can keep chatting.',
    generated:
      'All 6 shots are ready. Open the story in Works to watch it as a film, trim and export.',
    joeAdded: 'Added Grandpa Joe. Want him in a shot at the end, finding them together?',
    joeChips: ['Yes, add a last shot', 'No, keep it'],
    lastShot: 'Grandpa Joe steps out of the corn, drops his lantern and smiles.',
    lastShotAdded: 'Added shot 7 at the end. The film is now 35 s.',
    shot3:
      'What should change in shot 3? For example: “make it from Zib’s view, peeking through the corn”.',
    shot3Done: 'Updated shot 3: we now see Mia through Zib’s eyes, peeking between the stalks.',
    shot3Text: 'Through Zib’s eyes: Mia pushes through the corn towards us, torch shaking.',
    files: (count) =>
      `Added ${count} ${count === 1 ? 'picture' : 'pictures'} to the story. I used the first one as Mia’s look — click her card if it should be someone else.`,
    fallback: 'Got it. Tell me what to change, or pick one of the suggestions below.',
    redraw: 'Redraw Zib with a fresh look',
    picture: (name) => `Draw a picture of ${name}`,
    oneShot: (order) => `Generate shot ${order}, Standard quality`,
    declined: 'No problem, nothing was charged. Tell me when you are ready.',
  },
  zh: {
    title: '玉米地里的第一次相遇',
    zib: ['齐布', '绿色的小外星人，大大的黑眼睛，银色太空服；好奇，又有点害怕。'],
    mia: ['米娅', '八岁的小女孩，红色雨衣和雨靴；勇敢，手里拿着手电筒。'],
    joe: ['乔爷爷', '米娅的爷爷，格子衬衫，慢悠悠很和善；一开始不相信她。'],
    field: ['夜晚的玉米地', '高高的玉米秆，一轮满月，地面薄雾，远处农舍亮着灯。'],
    pod: ['坠落的飞船', '一艘小圆飞船半埋在土里，嗡嗡作响，发出青绿色的光。'],
    shots: [
      ['远景：一道青绿色的光落进玉米地。', 'field', 'pod'],
      ['米娅在卧室窗前看到亮光，抓起手电筒。', 'mia'],
      ['米娅钻进玉米地，嗡嗡声越来越响。', 'mia', 'field'],
      ['特写：飞船舱门打开，黑暗里两只大黑眼睛眨了眨。', 'zib', 'pod'],
      ['米娅慢慢伸出手，齐布学着她伸手。', 'mia', 'zib'],
      ['两人指尖相碰，整片玉米地亮起青绿色的光。', 'mia', 'zib', 'field'],
    ],
    hello:
      '好故事！我先建好了卡片：两个角色、一个地点和那艘飞船。片子想要多长？氛围是搞笑、温暖还是有点吓人？',
    helloChips: ['30 秒，温暖', '改成搞笑的', '再加一个角色'],
    shotsMade:
      '分好了 6 个镜头，每个 5 秒，一共 30 秒。点任何卡片都能改。要不要先画出齐布、米娅和玉米地，让每个镜头里的样子都一致？',
    shotsChips: ['先画角色', '改一下第 3 个镜头', '生成全部镜头'],
    drawQuote: '画出 2 个角色、1 个地点和飞船',
    drawn: '画好了，看看卡片。哪张脸不对，点开告诉我怎么改。准备好生成镜头了吗？',
    drawnChips: ['生成全部镜头', '重画齐布'],
    genQuote: '生成 6 个镜头，标准画质',
    generating: '正在生成，做好一个，卡片上就出现一个。你可以继续聊。',
    generated: '6 个镜头都好了。到作品里打开这个故事，可以连起来看、剪辑和导出。',
    joeAdded: '加上了乔爷爷。要不要在最后加一个镜头，让他找到他们俩？',
    joeChips: ['好，加在最后', '不用了'],
    lastShot: '乔爷爷从玉米地里走出来，手里的提灯掉在地上，他笑了。',
    lastShotAdded: '在最后加了第 7 个镜头，现在一共 35 秒。',
    shot3: '第 3 个镜头想怎么改？比如：“换成齐布的视角，从玉米秆缝里偷看”。',
    shot3Done: '改好了第 3 个镜头：现在是齐布的视角，从玉米秆之间看米娅走过来。',
    shot3Text: '齐布的视角：米娅拨开玉米秆朝我们走来，手电筒的光在晃。',
    files: (count) =>
      `把 ${count} 张图片加进了故事。第一张我先当作米娅的样子，不对的话点她的卡片换掉。`,
    fallback: '收到。告诉我想改哪里，或者点下面的建议。',
    redraw: '重画齐布，换个样子',
    picture: (name) => `画出${name}`,
    oneShot: (order) => `生成镜头 ${order}，标准画质`,
    declined: '好的，没有扣费。准备好了再告诉我。',
  },
};

const tints = { zib: '#7bd88f', mia: '#e5534b', joe: '#c9a36a', field: '#3b5b7a', pod: '#3fd1c4' };

// A flat placeholder picture so the preview shows where generated images land.
export function placeholderImage(label, tint = '#5a6b60', wide = false) {
  const [w, h] = wide ? [320, 180] : [240, 240];
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${tint}"/><stop offset="1" stop-color="#101412"/></linearGradient></defs><rect width="100%" height="100%" fill="url(#g)"/><text x="50%" y="54%" font-family="sans-serif" font-size="${wide ? 22 : 64}" fill="#ffffffcc" text-anchor="middle">${label}</text></svg>`;
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
}

const SHOT_CREDITS = 20,
  IMAGE_CREDITS = 2;

export function createPreviewChat({ locale = 'en', pace = 1 } = {}) {
  const c = copy[locale] || copy.en;
  let version = 0,
    sequence = 0,
    stage = 'start',
    shots = 0,
    timers = [];
  const id = (prefix) => `${prefix}-${++sequence}`;
  const later = (ms, fn) => timers.push(setTimeout(fn, ms * pace));
  const profile = (key, kind) => ({
    op: 'upsert',
    card: { id: key, kind, name: c[key][0], text: c[key][1] },
  });
  const shotCard = (index, [text, ...refs]) => ({
    op: 'upsert',
    card: {
      id: `shot-${index + 1}`,
      kind: 'shot',
      order: index + 1,
      text,
      refs,
      start: index * 5,
      end: index * 5 + 5,
      status: 'draft',
    },
  });

  function reply(emit, steps) {
    emit({ type: 'turn.started' });
    let at = 500;
    for (const step of steps) {
      later(at, () => emit(step()));
      at += 650;
    }
    later(at, () => emit({ type: 'turn.finished' }));
  }
  const say =
    (text, chips = []) =>
    () => ({ type: 'assistant.message', id: id('a'), text, chips });
  const change =
    (changes, summary, extra = {}) =>
    () => ({
      type: 'project.changed',
      id: id('c'),
      version: ++version,
      changes,
      summary,
      ...extra,
    });
  const quote = (action, credits, label) => () => ({
    type: 'quote.proposed',
    id: id('q'),
    action,
    credits,
    label,
  });

  function send({ text = '', attachments = [] }, emit) {
    emit({ type: 'user.message', id: id('u'), text, attachments });
    const lower = text.toLowerCase();
    if (attachments.length) {
      reply(emit, [
        change(
          [
            {
              op: 'upsert',
              card: {
                id: 'mia',
                kind: 'character',
                name: c.mia[0],
                text: c.mia[1],
                image: attachments[0].url,
              },
            },
          ],
          { kind: 'material', count: attachments.length },
        ),
        say(c.files(attachments.length)),
      ]);
      return;
    }
    if (stage === 'start') {
      stage = 'cast';
      reply(emit, [
        change(
          [profile('zib', 'character'), profile('mia', 'character')],
          { kind: 'character', count: 2 },
          { title: c.title },
        ),
        change([profile('field', 'place'), profile('pod', 'prop')], { kind: 'place', count: 2 }),
        say(c.hello, c.helloChips),
      ]);
      return;
    }
    if (/character|角色/.test(lower) && /add|加/.test(lower)) {
      stage = 'joe';
      reply(emit, [
        change([profile('joe', 'character')], { kind: 'character', count: 1 }),
        say(c.joeAdded, c.joeChips),
      ]);
      return;
    }
    if (stage === 'joe' && shots > 0 && /yes|好|add|加/.test(lower)) {
      stage = 'shots';
      const last = shotCard(shots, [c.lastShot, 'joe', 'mia', 'zib']);
      shots += 1;
      reply(emit, [change([last], { kind: 'shot', count: 1 }), say(c.lastShotAdded, c.shotsChips)]);
      return;
    }
    if (stage === 'cast' || (stage === 'joe' && shots === 0)) {
      stage = 'shots';
      shots = c.shots.length;
      reply(emit, [
        change(
          c.shots.map((shot, index) => shotCard(index, shot)),
          { kind: 'shot', count: shots },
        ),
        say(c.shotsMade, c.shotsChips),
      ]);
      return;
    }
    if (/shot 3|第 ?3/.test(lower)) {
      stage = 'shot3';
      reply(emit, [say(c.shot3)]);
      return;
    }
    if (stage === 'shot3') {
      stage = 'shots';
      reply(emit, [
        change([{ op: 'upsert', card: { id: 'shot-3', text: c.shot3Text } }], {
          kind: 'edit',
          count: 1,
        }),
        say(c.shot3Done, c.shotsChips),
      ]);
      return;
    }
    if (/draw|picture|画|redraw|重画/.test(lower)) {
      const redraw = /redraw|重画/.test(lower);
      reply(emit, [
        quote(
          { kind: 'image', cards: redraw ? ['zib'] : ['zib', 'mia', 'field', 'pod'] },
          (redraw ? 1 : 4) * IMAGE_CREDITS,
          redraw ? c.redraw : c.drawQuote,
        ),
      ]);
      return;
    }
    if (/generate|生成/.test(lower)) {
      reply(emit, [quote({ kind: 'generate', cards: null }, shots * SHOT_CREDITS, c.genQuote)]);
      return;
    }
    reply(emit, [say(c.fallback, stage === 'shots' ? c.shotsChips : c.helloChips)]);
  }

  function settle(quote, accepted, cards, emit) {
    emit({ type: 'quote.settled', id: quote.id, status: accepted ? 'confirmed' : 'declined' });
    if (!accepted) {
      reply(emit, [say(c.declined)]);
      return;
    }
    if (quote.action.kind === 'image') {
      reply(emit, [
        ...quote.action.cards.map((key) => () => ({
          type: 'project.changed',
          id: id('c'),
          version: ++version,
          changes: [
            {
              op: 'upsert',
              card: { id: key, image: placeholderImage(c[key][0].slice(0, 2), tints[key]) },
            },
          ],
        })),
        say(c.drawn, c.drawnChips),
      ]);
      return;
    }
    const targets =
      quote.action.cards || cards.filter((card) => card.kind === 'shot').map((card) => card.id);
    emit({
      type: 'project.changed',
      id: id('c'),
      version: ++version,
      changes: targets.map((key) => ({ op: 'upsert', card: { id: key, status: 'generating' } })),
    });
    reply(emit, [say(c.generating)]);
    targets.forEach((key, index) =>
      later(1800 + index * 1100, () =>
        emit({
          type: 'project.changed',
          id: id('c'),
          version: ++version,
          changes: [
            {
              op: 'upsert',
              card: {
                id: key,
                status: 'ready',
                image: placeholderImage(`#${key.split('-')[1]}`, '#2f6f62', true),
              },
            },
          ],
        }),
      ),
    );
    if (!quote.action.cards) later(2400 + targets.length * 1100, () => emit(say(c.generated)()));
  }

  // A paid action the user starts from a card rather than from the chat.
  function propose(action, label, emit) {
    const count = action.cards.length;
    emit(quote(action, count * (action.kind === 'image' ? IMAGE_CREDITS : SHOT_CREDITS), label)());
  }
  const labels = {
    image: (card) => c.picture(card.name),
    generate: (card) => c.oneShot(card.order),
  };

  return {
    balance: 300,
    propose: (action, card, emit) => propose(action, labels[action.kind](card), emit),
    send,
    settle,
    // A card the user edits: the server bumps the version the same way.
    edited: () => ++version,
    close: () => timers.forEach(clearTimeout),
  };
}
