// Terms, Privacy and Content Policy (h3-studio#308), owner-reviewed 2026-10-28 (h3-studio#374).
// While LEGAL_STATUS is 'draft', every page shows a banner saying it is not yet in effect. Bump `version` on any material change; the backend
// asks users to accept the new Terms version once (#305 terms_required).
// Blocks: ['h', text] heading · ['p', text] paragraph · ['ul', [items]] list.

export const LEGAL_STATUS = 'reviewed';
export const SUPPORT_EMAIL = 'support@inkseq.com';

export const LEGAL = {
  terms: {
    version: '2026-10-28',
    en: {
      title: 'Terms of Service',
      blocks: [
        [
          'p',
          'These terms are an agreement between you and Inkseq ("we", "us"), for the Inkseq video creation studio at inkseq.com, its API and its AI assistant connections (together, "the service"). By creating an account or using the service you agree to them.',
        ],
        ['h', '1. Your account'],
        [
          'ul',
          [
            'You must be at least 18 years old.',
            'You can sign in with Google, GitHub, email and password, or an emailed code. Keep your sign-in methods and API keys safe; you are responsible for activity on your account.',
            'AI assistants and API keys you connect act on your behalf. Their actions, and the credits they use, count as yours. You can set a daily cap for each one and disconnect it at any time.',
            'One person per account. Do not share accounts or create accounts to get extra welcome credits.',
          ],
        ],
        ['h', '2. Credits and payment'],
        [
          'ul',
          [
            'Generation is paid with prepaid credits. We show the number of credits before you submit, and the price you were quoted is the price you pay.',
            'Credit pack prices are in US dollars and exclude tax. Any sales tax, VAT or GST that applies where you live is added at checkout.',
            'Purchased credits do not expire. Credits have no cash value outside the service and cannot be transferred or resold.',
            'Promotional credits, such as welcome credits, the bonus credits included in a pack or credits from a campaign, are free. They may come with their own conditions or expiry, which we show when you receive them, and they can be removed if they were obtained in breach of these terms.',
            'Generations that fail on our side are returned to your balance automatically.',
            'Purchases are final and credits are not refundable, except where the law requires (for example under the Australian Consumer Law). If a purchase is refunded, the credits from it, including its bonus credits, are removed from your balance.',
            'Payments are processed by Stripe. For credit purchases Stripe acts as the reseller (merchant of record): it charges you, calculates any sales tax, VAT or GST, and issues your receipt, and its checkout terms also apply to the purchase. We never see or store your card number.',
            'We may change prices. A change applies to new quotes only, never to work you have already submitted.',
            'Payments from mainland China are not supported at the moment.',
          ],
        ],
        ['h', '3. Your content'],
        [
          'ul',
          [
            'You keep the rights you have in what you upload (text, images, audio, video) and, to the extent the law allows, in what you generate.',
            'You give us a limited licence to store, process and display your content only to run the service for you: for example, to send your inputs to a generation machine, store the results, and show them to you and to people you choose to share them with.',
            'We do not use your prompts or media to train AI models without your permission.',
            "AI output can resemble other people's work and can contain mistakes. You are responsible for checking it and for how you use it, including any rights you need for characters, people, music or brands that appear in it.",
            'Your content must follow the Content Policy.',
          ],
        ],
        ['h', '4. Acceptable use'],
        [
          'ul',
          [
            "Do not break the law, infringe other people's rights or break the Content Policy.",
            "Do not attack, overload or probe the service, get around limits or caps, or access other users' data.",
            'Automated use is welcome through the documented API and AI assistant connections, within your caps.',
          ],
        ],
        ['h', '5. Suspension and closing your account'],
        [
          'ul',
          [
            'You can close your account in Settings. Your works and credit records are kept for 30 days in case you change your mind, then deleted, except records we must keep by law (for example payment records).',
            'We may suspend or close an account that breaks these terms or the Content Policy, or to protect users or the service. Where it is safe to do so we will tell you why. Credits obtained or used in breach of these terms may be forfeited.',
          ],
        ],
        ['h', '6. The service'],
        [
          'p',
          'The service is under active development. Features, models and limits can change, and generations can occasionally fail or be slow. We work to keep it available and your work safe, but we cannot promise it will be uninterrupted or error-free.',
        ],
        ['h', '7. Liability'],
        [
          'p',
          'Nothing in these terms excludes rights you have under consumer protection law, including the Australian Consumer Law, that cannot be excluded. Apart from those rights, and to the extent the law allows, we are not liable for indirect or consequential loss, and our total liability to you is limited to the amount you paid us in the 12 months before the claim.',
        ],
        ['h', '8. Changes and contact'],
        [
          'p',
          'If we change these terms in a material way we will tell you in the service and ask you to accept the new version before your next paid generation. These terms are governed by the laws of Victoria, Australia. Questions: support@inkseq.com.',
        ],
      ],
    },
    zh: {
      title: '服务条款',
      blocks: [
        [
          'p',
          '本条款是你与映序 Inkseq（下称「我们」）之间关于 inkseq.com 视频创作工作台、API 以及 AI 助手连接（合称「本服务」）的协议。创建账号或使用本服务，即表示你同意本条款。',
        ],
        ['h', '1. 你的账号'],
        [
          'ul',
          [
            '你必须年满 18 岁。',
            '你可以用 Google、GitHub、邮箱密码或邮件验证码登录。请保管好登录方式和 API 密钥；账号上的操作由你负责。',
            '你连接的 AI 助手和 API 密钥代表你操作，它们的操作和用掉的积分都算你的。你可以给每个助手单独设每日上限，也可以随时断开。',
            '一人一个账号。不要共用账号，也不要为了多拿新用户积分而注册多个账号。',
          ],
        ],
        ['h', '2. 积分与付款'],
        [
          'ul',
          [
            '生成使用预付积分。提交前我们会告诉你要用多少积分，按报价扣，不会多扣。',
            '积分包价格以美元计，不含税。你所在地适用的销售税、增值税或 GST 会在结账时另加。',
            '购买的积分不会过期。积分不能在本服务以外兑现，也不能转让或转卖。',
            '赠送积分（例如新用户积分、积分包附赠的积分、活动积分）是免费的，可能有各自的使用条件或有效期，会在发放时告诉你；以违反本条款的方式获得的赠送积分可以被收回。',
            '因我们这边原因失败的生成，积分自动退回。',
            '购买后不退款，积分不能退，法律另有规定的除外（例如《澳大利亚消费者法》）。如果某笔购买被退款，这笔购买得到的积分（包括附赠积分）会从余额中扣回。',
            '付款由 Stripe 处理。购买积分时，Stripe 作为转售方（merchant of record）向你收款、计算适用的销售税／增值税／GST 并开具收据，这次购买也适用 Stripe 的结账条款。我们看不到也不保存你的卡号。',
            '我们可能调整价格。调价只影响新的报价，不影响你已经提交的任务。',
            '暂时不支持中国大陆的付款。',
          ],
        ],
        ['h', '3. 你的内容'],
        [
          'ul',
          [
            '你上传的内容（文字、图片、音频、视频）的权利仍归你；在法律允许的范围内，你生成的内容也归你。',
            '你授予我们有限的许可，仅为了给你提供服务而存储、处理和展示你的内容，例如把输入发送到生成机器、保存结果、展示给你和你选择分享的人。',
            '未经你同意，我们不会用你的提示词或素材训练 AI 模型。',
            'AI 生成的内容可能与他人作品相似，也可能有错误。你需要自行检查，并对使用方式负责，包括其中出现的角色、真人、音乐或品牌所需的权利。',
            '你的内容必须遵守《内容规范》。',
          ],
        ],
        ['h', '4. 使用规则'],
        [
          'ul',
          [
            '不得违法、侵犯他人权利或违反《内容规范》。',
            '不得攻击、压垮或探测本服务，不得绕过限制或上限，不得访问其他用户的数据。',
            '欢迎通过公开的 API 和 AI 助手连接自动化使用，但须在你设定的上限之内。',
          ],
        ],
        ['h', '5. 停用与注销'],
        [
          'ul',
          [
            '你可以在设置里注销账号。作品和积分记录会保留 30 天，期间可以撤回，之后删除；法律要求保留的记录（例如付款记录）除外。',
            '如果账号违反本条款或《内容规范》，或为保护用户和服务所需，我们可以停用或关闭账号；在安全的情况下会告诉你原因。违规获得或违规使用的积分可能被收回。',
          ],
        ],
        ['h', '6. 关于本服务'],
        [
          'p',
          '本服务仍在积极开发中，功能、模型和限制可能变化，生成偶尔会失败或变慢。我们会尽力保持服务可用、作品安全，但不能保证服务不中断、不出错。',
        ],
        ['h', '7. 责任'],
        [
          'p',
          '本条款不排除你依消费者保护法（包括《澳大利亚消费者法》）享有的、不能被排除的权利。除这些权利外，在法律允许的范围内，我们不对间接或后果性损失负责，对你的总责任以索赔前 12 个月内你向我们支付的金额为限。',
        ],
        ['h', '8. 条款变更与联系'],
        [
          'p',
          '如果本条款有重大变更，我们会在服务中告诉你，并在你下一次付费生成前请你接受新版本。本条款适用澳大利亚维多利亚州法律。有问题请写信到 support@inkseq.com。',
        ],
      ],
    },
  },
  privacy: {
    version: '2026-10-28',
    en: {
      title: 'Privacy Policy',
      blocks: [
        [
          'p',
          'This policy explains what personal information Inkseq collects, why, who processes it for us, and the choices you have. We collect only what we need to run the service.',
        ],
        ['h', 'What we collect'],
        [
          'ul',
          [
            'Account: your email address, display name, and the identifier from the sign-in provider you use (Google, GitHub or email).',
            'Your content: prompts, stories, scripts, and the images, audio and video you upload or generate.',
            'Payments: the pack, amount, status and receipt of each purchase. Your card details go to our payment provider and never reach us.',
            'Usage and technical data: generation jobs and their cost in credits, request logs, IP address, browser type and error reports, used to run, secure and fix the service.',
            'Browser storage: a sign-in session cookie, plus settings such as your language kept in your browser. We do not use advertising or cross-site tracking cookies.',
          ],
        ],
        ['h', 'How we use it'],
        [
          'ul',
          [
            'To provide the service: sign you in, run your generations, store your works, and keep your credit balance.',
            'To keep it safe: prevent abuse and fraud, and enforce the Terms and Content Policy.',
            'To support you and to send service messages such as sign-in codes, receipts and important changes. We do not send marketing email without your consent.',
            'To improve reliability and speed, using logs and aggregated metrics.',
            'We do not sell your personal information, and we do not use your prompts or media to train AI models without your permission.',
          ],
        ],
        ['h', 'Who processes it for us'],
        [
          'ul',
          [
            'Infrastructure providers for hosting, databases, email and file storage.',
            'A sign-in provider for account security, and our payment provider for purchases and tax.',
            'Product analytics and error monitoring. Analytics may replay some page visits with all typed text masked; we do not send prompts or story text, and error reports do not include what you type.',
            'Rented GPU machines that run your generations. Your inputs are copied to the machine while it works and can stay in its temporary cache until it shuts down; machines are shut down when idle, and your works are kept in our own storage.',
            'AI model providers, when you use story import or an AI assistant feature: the relevant text is sent to produce a response.',
          ],
        ],
        [
          'p',
          'These providers may process data outside your country. They may use it only to provide their service to us.',
        ],
        ['h', 'How long we keep it'],
        [
          'ul',
          [
            'Account data and works: while your account is open. After you ask to close your account we wait 30 days, so you can change your mind, and then delete them.',
            'Payment and tax records: as long as the law requires.',
            'Logs: up to 30 days, then deleted or reduced to aggregate numbers.',
          ],
        ],
        ['h', 'Your choices and rights'],
        [
          'p',
          'You can view and edit your account in Settings, download or delete your works, disconnect sign-in methods and AI assistants, and close your account. You can also ask us for a copy of your personal information or to correct or delete it by writing to support@inkseq.com. If you are not satisfied with our answer, you can complain to your local privacy regulator; in Australia, that is the Office of the Australian Information Commissioner (OAIC).',
        ],
        ['h', 'Security'],
        [
          'p',
          "We encrypt connections, keep credentials in a managed secrets store, limit staff access, and keep each account's data separate. No system is perfectly secure; if a breach affects you we will tell you as the law requires.",
        ],
        ['h', 'Children'],
        [
          'p',
          'The service is not for people under 18, and we do not knowingly collect their data.',
        ],
        ['h', 'Changes and contact'],
        [
          'p',
          'We will post changes here and tell you in the service about material ones. Questions or requests: support@inkseq.com.',
        ],
      ],
    },
    zh: {
      title: '隐私政策',
      blocks: [
        [
          'p',
          '本政策说明映序（Inkseq）收集哪些个人信息、为什么收集、由谁替我们处理，以及你有哪些选择。我们只收集运行服务所需的信息。',
        ],
        ['h', '我们收集什么'],
        [
          'ul',
          [
            '账号：邮箱、显示名字，以及你所用登录方式（Google、GitHub 或邮箱）提供的标识。',
            '你的内容：提示词、故事、剧本，以及你上传或生成的图片、音频和视频。',
            '付款：每次购买的积分包、金额、状态和收据。卡号由付款服务商处理，不经过我们。',
            '使用和技术数据：生成任务及其积分花费、请求日志、IP 地址、浏览器类型和错误报告，用于运行、保护和修复服务。',
            '浏览器存储：登录会话 cookie，以及保存在你浏览器里的语言等设置。我们不使用广告或跨站追踪 cookie。',
          ],
        ],
        ['h', '我们怎么用'],
        [
          'ul',
          [
            '提供服务：登录、运行生成、保存作品、记录积分余额。',
            '保障安全：防止滥用和欺诈，执行《服务条款》和《内容规范》。',
            '为你提供支持，并发送服务消息，例如登录验证码、收据和重要变更。未经同意不发营销邮件。',
            '用日志和汇总指标改进稳定性和速度。',
            '我们不出售你的个人信息；未经你同意，不用你的提示词或素材训练 AI 模型。',
          ],
        ],
        ['h', '谁替我们处理'],
        [
          'ul',
          [
            '基础设施服务商：托管、数据库、邮件和文件存储。',
            '登录服务商负责账号安全；付款服务商负责购买和税务。',
            '产品使用统计和错误监控。统计可能回放部分页面操作，所有输入的文字都会被遮挡；我们不发送提示词或故事内容，错误报告也不包含你输入的文字。',
            '运行生成的租用 GPU 机器。生成期间你的输入会被复制到这台机器上，可能留在临时缓存里直到机器关机；机器空闲时会关机，你的作品保存在我们自己的存储里。',
            'AI 模型服务商：使用故事导入或 AI 助手功能时，相关文字会发送给模型服务商以生成回复。',
          ],
        ],
        ['p', '这些服务商可能在你所在国家以外处理数据。它们只能为向我们提供服务而使用这些数据。'],
        ['h', '保留多久'],
        [
          'ul',
          [
            '账号数据和作品：账号存在期间保留。你申请注销后等待 30 天（期间可以撤回），然后删除。',
            '付款和税务记录：按法律要求的期限保留。',
            '日志：最多保留 30 天，之后删除或只保留汇总数字。',
          ],
        ],
        ['h', '你的选择和权利'],
        [
          'p',
          '你可以在设置里查看和修改账号、下载或删除作品、断开登录方式和 AI 助手，以及注销账号。也可以写信到 support@inkseq.com，要求获取你的个人信息副本，或更正、删除这些信息。如果对我们的答复不满意，可以向当地隐私监管机构投诉；在澳大利亚是澳大利亚信息专员办公室（OAIC）。',
        ],
        ['h', '安全'],
        [
          'p',
          '我们加密网络连接，把凭据保存在托管的密钥库里，限制员工访问，并把每个账号的数据隔离开。没有绝对安全的系统；如果发生影响你的数据泄露，我们会按法律要求通知你。',
        ],
        ['h', '未成年人'],
        ['p', '本服务不面向 18 岁以下的人，我们不会有意收集他们的数据。'],
        ['h', '变更与联系'],
        [
          'p',
          '变更会发布在这里，重大变更会在服务中通知你。问题或请求请写信到 support@inkseq.com。',
        ],
      ],
    },
  },
  content: {
    version: '2026-10-28',
    en: {
      title: 'Content Policy',
      blocks: [
        [
          'p',
          'Inkseq is for telling stories with video. These rules apply to everything you upload, generate, store or share on the service, including through the API and AI assistants.',
        ],
        ['h', 'Never allowed'],
        [
          'ul',
          [
            'Any sexual content involving minors, or characters that appear to be minors, in any style, including anime and cartoons.',
            'Sexually explicit content of any kind. Inkseq is not for adult content.',
            'Sexual or intimate content of a real person, or content made to look like a real person without their consent.',
            'Deceptive deepfakes: realistic media of real people saying or doing things they did not, made to mislead.',
            'Content that threatens, harasses or bullies a person, or promotes hatred against people because of who they are.',
            'Content that promotes terrorism, or gives real instructions for serious violence or weapons that can cause mass harm.',
            'Content that encourages suicide, self-harm or eating disorders.',
            'Anything else that is illegal where you live or in Australia.',
          ],
        ],
        ['h', "Other people's rights"],
        [
          'ul',
          [
            "Use a real person's face, voice or likeness only with their permission.",
            'Characters, music, logos and other works you do not own are fine for private experiments. Do not publish or sell them unless you hold the rights.',
          ],
        ],
        ['h', 'Violence and mature themes'],
        [
          'p',
          'Stories can be dark. Action, conflict, horror and mature themes are fine when they serve the story and do not fall into the categories above.',
        ],
        ['h', 'How we enforce'],
        [
          'ul',
          [
            'We may check prompts and outputs automatically before and after generation. If a request is refused for policy reasons, you are not charged.',
            'We may remove content that breaks this policy, and suspend or close accounts. Serious or repeated violations can lead to permanent closure, and credits used or obtained in violation may be forfeited.',
            'We report child sexual abuse material to the authorities.',
          ],
        ],
        ['h', 'Report a problem'],
        [
          'p',
          'If you see content on Inkseq that breaks these rules, or that uses your likeness or work without permission, write to support@inkseq.com with a link. We review every report.',
        ],
      ],
    },
    zh: {
      title: '内容规范',
      blocks: [
        [
          'p',
          '映序是用视频讲故事的地方。以下规则适用于你在本服务上传、生成、保存或分享的一切内容，包括通过 API 和 AI 助手产生的内容。',
        ],
        ['h', '绝对不允许'],
        [
          'ul',
          [
            '任何涉及未成年人、或看起来像未成年人的角色的性内容，无论什么画风，包括动漫和卡通。',
            '任何露骨的色情内容。映序不提供成人内容。',
            '真人的性或私密内容，或未经本人同意、做得像某个真人的此类内容。',
            '欺骗性深度伪造：以误导为目的，逼真地呈现真人说了或做了他们没说没做的事。',
            '威胁、骚扰或欺凌他人，或因身份宣扬对他人的仇恨。',
            '宣扬恐怖主义，或提供可造成严重暴力、大规模伤害的武器的真实操作方法。',
            '鼓励自杀、自残或进食障碍。',
            '其他在你所在地或澳大利亚违法的内容。',
          ],
        ],
        ['h', '他人的权利'],
        [
          'ul',
          [
            '使用真人的脸、声音或形象，须得到本人许可。',
            '不属于你的角色、音乐、标志和其他作品，可以用于私人尝试；除非你拥有权利，否则不要发布或出售。',
          ],
        ],
        ['h', '暴力和成熟题材'],
        [
          'p',
          '故事可以很黑暗。动作、冲突、恐怖和成熟题材都可以，只要是为故事服务，并且不属于上面不允许的类别。',
        ],
        ['h', '我们怎么执行'],
        [
          'ul',
          [
            '我们可能在生成前后自动检查提示词和结果。因内容规范被拒绝的请求，不扣积分。',
            '我们可以删除违反本规范的内容，并停用或关闭账号。严重或屡次违规可能导致永久关闭，违规使用或获得的积分可能被收回。',
            '涉及儿童性虐待的内容，我们会向有关部门报告。',
          ],
        ],
        ['h', '举报问题'],
        [
          'p',
          '如果你在映序看到违反这些规则的内容，或有人未经许可使用了你的形象或作品，请把链接发到 support@inkseq.com。每一条举报我们都会处理。',
        ],
      ],
    },
  },
};
