export const trashCatalogs = {
  en: {
    'trash.menu': 'More',
    'trash.title': 'Trash',
    'trash.rename': 'Rename',
    'trash.edit': 'Edit',
    'trash.move': 'Delete',
    'trash.selectStory': 'Select story',
    'trash.selectedCount': '{count} selected',
    'trash.selectAll': 'Select all',
    'trash.moveSelected': 'Delete {count}',
    'trash.editInChat': 'Edit in chat',
    'trash.restore': 'Restore',
    'trash.purge': 'Delete forever',
    'trash.empty': 'Empty trash',
    'trash.undo': 'Undo',
    'trash.moved': 'Deleted. You can restore it from Trash',
    'trash.confirm': 'Delete the selected items forever?',
    'trash.irreversible': "This can't be undone. Generation and cost records are kept.",
    'trash.cancel': 'Cancel',
    'trash.all': 'All kinds',
    'trash.selectAll': 'Select all',
    'trash.more': 'Load more',
    'trash.none': 'Trash is empty',
    'trash.quickElsewhere': 'See {count} Quick Create chats',
    'trash.noneOutsideQuick': 'No deleted stories or files',
    'trash.signIn': 'Sign in to see your trash.',
    'trash.pending': 'The last action is still being checked.',
    'trash.check': 'Check last action',
    'trash.resume': 'Continue last action',
    'trash.failed': "Couldn't confirm. Your selection is kept.",
    'trash.running': 'Something is still running.',
    'trash.minimum': 'Keep at least one chapter and one scene.',
    'trash.changed': 'This changed. Refresh and select again.',
    'trash.refresh': 'Refresh',
    'trash.saved': 'Saved',
    'trash.save': 'Save',
    'trash.name': 'Name',
    'trash.restored': 'Restored',
    'trash.deleted': 'Deleted forever',
    'trash.unavailable': "You can't do that to this item right now.",
  },
  zh: {
    'trash.menu': '更多',
    'trash.title': '回收站',
    'trash.rename': '重命名',
    'trash.edit': '编辑',
    'trash.move': '删除',
    'trash.selectStory': '选择故事',
    'trash.selectedCount': '已选 {count} 个',
    'trash.selectAll': '全选',
    'trash.moveSelected': '删除 {count} 个',
    'trash.editInChat': '在聊天里改',
    'trash.restore': '恢复',
    'trash.purge': '永久删除',
    'trash.empty': '清空回收站',
    'trash.undo': '撤销',
    'trash.moved': '已删除，可在回收站恢复',
    'trash.confirm': '永久删除所选内容？',
    'trash.irreversible': '删除后无法恢复。生成和费用记录会保留。',
    'trash.cancel': '取消',
    'trash.all': '全部类型',
    'trash.selectAll': '全选',
    'trash.more': '加载更多',
    'trash.none': '回收站是空的',
    'trash.quickElsewhere': '查看快速创作里的 {count} 个对话',
    'trash.noneOutsideQuick': '没有删除的故事或文件',
    'trash.signIn': '登录后查看回收站。',
    'trash.pending': '上一个操作还在确认，请稍等。',
    'trash.check': '查看上一个操作',
    'trash.resume': '继续上一个操作',
    'trash.failed': '没能确认结果，选择已保留。',
    'trash.running': '还有任务在运行或还没出结果。',
    'trash.minimum': '至少要留一个章节和一个场景。',
    'trash.changed': '内容变了，请刷新后重新选择。',
    'trash.refresh': '刷新',
    'trash.saved': '已保存',
    'trash.save': '保存',
    'trash.name': '名称',
    'trash.restored': '已恢复',
    'trash.deleted': '已永久删除',
    'trash.unavailable': '现在不能对它这样操作。',
  },
};

Object.assign(trashCatalogs.en, {
  'trash.close': 'Dismiss',
  'trash.unsaved': 'Save your changes before deleting.',
  'trash.untitled': 'Untitled',
  'trash.retention':
    "Deleted items stay here until you delete them forever. Restoring doesn't regenerate anything.",
  'trash.conversations': 'Manage conversation trash',
  'trash.summary': 'Removes {removed} files ({bytes} bytes); keeps {kept} shared files.',
});
Object.assign(trashCatalogs.zh, {
  'trash.close': '关闭',
  'trash.unsaved': '请先保存修改，再删除。',
  'trash.untitled': '未命名',
  'trash.retention': '删除的内容会一直留在这里，直到你永久删除。恢复不会重新生成。',
  'trash.conversations': '管理对话回收站',
  'trash.summary': '将删除 {removed} 个文件（{bytes} 字节），保留 {kept} 个共享文件。',
});
Object.assign(trashCatalogs.en, {
  'trash.kind.project': 'Story',
  'trash.kind.chapter': 'Chapter',
  'trash.kind.scene': 'Scene',
  'trash.kind.shot': 'Shot',
  'trash.kind.character': 'Character',
  'trash.kind.location': 'Location',
  'trash.kind.prop': 'Prop',
  'trash.kind.look': 'Look',
  'trash.kind.location_version': 'Location version',
  'trash.kind.image': 'Image',
  'trash.kind.video': 'Video',
  'trash.kind.audio': 'Audio',
  'trash.kind.asset': 'File',
  'trash.kind.artifact': 'Generated version',
  'trash.kind.take': 'Take',
});
Object.assign(trashCatalogs.zh, {
  'trash.kind.project': '故事',
  'trash.kind.chapter': '章节',
  'trash.kind.scene': '场景',
  'trash.kind.shot': '镜头',
  'trash.kind.character': '角色',
  'trash.kind.location': '地点',
  'trash.kind.prop': '道具',
  'trash.kind.look': '造型',
  'trash.kind.location_version': '地点版本',
  'trash.kind.image': '图片',
  'trash.kind.video': '视频',
  'trash.kind.audio': '音频',
  'trash.kind.asset': '文件',
  'trash.kind.artifact': '生成结果',
  'trash.kind.take': '生成的一条',
});

trashCatalogs.en['trash.cloudOnly'] = 'Open a saved story to use the trash.';
trashCatalogs.zh['trash.cloudOnly'] = '请先打开已保存的故事，才能用回收站。';
Object.assign(trashCatalogs.en, {
  'trash.manage': 'Manage story items',
  'trash.search': 'Find',
});
Object.assign(trashCatalogs.zh, { 'trash.manage': '管理故事内容', 'trash.search': '查找' });

Object.assign(trashCatalogs.en, {
  'trash.selectAll': 'Select all',
  'trash.emptyFiltered': 'Delete all of this kind forever',
});
Object.assign(trashCatalogs.zh, {
  'trash.selectAll': '全选',
  'trash.emptyFiltered': '永久删除这一类的全部内容',
});

trashCatalogs.en['trash.kind.conversation'] = 'Conversation';
trashCatalogs.zh['trash.kind.conversation'] = '对话';

trashCatalogs.en['trash.filters'] = 'Filter';
trashCatalogs.zh['trash.filters'] = '筛选';
