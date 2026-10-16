import {adjunctCatalogs} from './adjunctCatalogs.js';
import {accountDetailCatalogs} from './accountDetailCatalogs.js';
import {operatorPanelCatalogs} from './operatorPanelCatalogs.js';
import {h3MoreCatalogs} from './h3MoreCatalogs.js';
import {operatorReasonCatalogs} from './operatorReasonCatalogs.js';
import {operatorDetailCatalogs} from './operatorDetailCatalogs.js';
import {legacyOperatorCatalogs} from './legacyOperatorCatalogs.js';
import {operatorErrorCatalogs} from './operatorErrorCatalogs.js';
import {storyCatalogs} from './storyCatalogs.js';
import {operatorCatalogs} from './operatorCatalogs.js';
import {accountCatalogs} from './accountCatalogs.js';
import {workspaceCatalogs} from './workspaceCatalogs.js';
import {settingsCatalogs} from './settingsCatalogs.js';
import {clientCatalogs} from './clientCatalogs.js';
import {storyboardCatalogs} from './storyboardCatalogs.js';
import {legacyCatalogs} from './legacyCatalogs.js';
export const catalogs = {
  en: {
    ...adjunctCatalogs.en,
    ...h3MoreCatalogs.en,
    ...operatorPanelCatalogs.en,
    ...legacyOperatorCatalogs.en,
    ...operatorErrorCatalogs.en, ...operatorDetailCatalogs.en, ...operatorReasonCatalogs.en, ...accountDetailCatalogs.en, ...storyCatalogs.en, ...operatorCatalogs.en, ...accountCatalogs.en, ...workspaceCatalogs.en, ...settingsCatalogs.en, ...clientCatalogs.en, ...storyboardCatalogs.en, ...legacyCatalogs.en,
    'app.name': 'Inkseq',
    'app.libraryTitle': 'Inkseq · Library',
    'app.storyTitle': 'Inkseq · Story workspace',
    'nav.skipWorkspace': 'Skip to workspace',
    'nav.library': 'Library',
    'nav.mobile': 'Main navigation',
    'storyboard.modelUnloaded': 'Model capabilities have not been loaded',
    'storyboard.closeEditor': 'Close shot editor',
    'storyboard.changeFailed': 'This change did not complete. The original record is retained.',
    'storyboard.operationUnknown': 'This operation is unconfirmed. Reconcile its original record.',
    'nav.quickCreate': 'Quick Create',
    'nav.storyboard': 'Storyboard',
    'nav.guided': 'Guided workflow',
    'nav.canvas': 'Canvas',
    'nav.workspaceMode': 'Workspace mode',
    'nav.chapter': 'Current chapter',
    'loading.operator': 'Opening GPU console…',
    'loading.quickCreate': 'Opening Quick Create…',
    'locale.switchToChinese': 'Switch to Chinese',
    'locale.switchToEnglish': 'Switch to English',
  },
  zh: {
    ...adjunctCatalogs.zh,
    ...h3MoreCatalogs.zh,
    ...operatorPanelCatalogs.zh,
    ...legacyOperatorCatalogs.zh,
    ...operatorErrorCatalogs.zh, ...operatorDetailCatalogs.zh, ...operatorReasonCatalogs.zh, ...accountDetailCatalogs.zh, ...storyCatalogs.zh, ...operatorCatalogs.zh, ...accountCatalogs.zh, ...workspaceCatalogs.zh, ...settingsCatalogs.zh, ...clientCatalogs.zh, ...storyboardCatalogs.zh, ...legacyCatalogs.zh,
    'app.name': '映序',
    'app.libraryTitle': '映序 · 作品库',
    'app.storyTitle': '映序 · 故事工作台',
    'nav.skipWorkspace': '跳到工作区',
    'nav.library': '作品库',
    'nav.mobile': '主要导航',
    'storyboard.modelUnloaded': '服务能力尚未读取',
    'storyboard.closeEditor': '关闭镜头编辑',
    'storyboard.changeFailed': '这次修改没有完成，原记录已保留。',
    'storyboard.operationUnknown': '这次操作尚未确认，请核对原记录。',
    'nav.quickCreate': '快速创作',
    'nav.storyboard': '分镜',
    'nav.guided': '七步引导',
    'nav.canvas': '画布',
    'nav.workspaceMode': '工作方式',
    'nav.chapter': '当前章节',
    'loading.operator': '正在打开 GPU 控制台…',
    'loading.quickCreate': '正在打开聊天创作…',
    'locale.switchToChinese': '切换为中文',
    'locale.switchToEnglish': '切换为英文',
  },
};
