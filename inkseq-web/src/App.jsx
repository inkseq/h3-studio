import { Button } from './ui/Button.jsx';
import InkLoader from './InkLoader.jsx';
import StudioHeader from './StudioHeader.jsx';
import { lastAccount, rememberAccount } from './account-access.js';
import { SESSION_REFUSED } from './cloud-client.js';
import { LowCreditsDialog } from './CreditsUI.jsx';
import { TermsGate } from './TermsGate.jsx';
import { LegalPage, PricingPage } from './PublicPages.jsx';
import Landing from './Landing.jsx';
import { TrashProvider } from './trash-provider.jsx';
import React, { useEffect, useRef, useSyncExternalStore } from 'react';
import { LayoutGrid } from 'lucide-react';
// A page fetched when first opened, together with the words it shows.
const page = (load, words = 'app') =>
  React.lazy(() => Promise.all([load(), loadWords(words)]).then(([module]) => module));
const Freestyle = page(() => import('./Freestyle.jsx'));
const StoryChat = page(() => import('./StoryChat.jsx'));
const QuickCreateEntry = page(() => import('./QuickCreateEntry.jsx'));
const LegacyQuickChatLink = page(() => import('./LegacyQuickChatLink.jsx'));
const OperatorConsole = page(() => import('./P4OperatorConsole.jsx'), 'ops');
const AdminHome = page(() => import('./AdminHome.jsx'), 'ops');
import { useCloud } from './use-cloud.js';
import { cloudController as cloud } from './cloud-controller.js';
import { readAgentLink } from './agent-navigation.js';
import { store } from './store.js';
import PersistenceWarning from './PersistenceWarning.jsx';
import { installPersistenceGuard } from './persistence-guard.js';
import { I18nProvider, useI18n } from './i18n/I18nProvider.jsx';
import { loadWords } from './i18n/locale.js';
import AppRouteCache, { useAppLocation } from './AppRouteCache.jsx';
import MaintenanceBanner from './MaintenanceBanner.jsx';
import FloatingNotices from './FloatingNotices.jsx';
import PageErrorBoundary from './PageErrorBoundary.jsx';
import { installAppNavigation, navigateApp } from './app-navigation.js';
import { createStoryRouteLoader, openedStoryHref } from './story-open.js';
// The story workspace, its pages and the admin code stay out of the first script, so the public
// pages paint quickly. Each loads with the page that needs it.
const SeriesApp = page(() => import('./SeriesApp.jsx'));
const ConnectTelegramPage = page(() => import('./ConnectTelegramPage.jsx'));
const Library = page(() => import('./Library.jsx'));
const UniversalTrash = page(() => import('./UniversalTrash.jsx'));
const SpendingSettings = page(() => import('./SpendingSettings.jsx'));
const StoryOpening = page(() =>
  import('./SeriesApp.jsx').then((module) => ({ default: module.StoryOpening })),
);
// Replace the address with another page once rendered (old links that moved).
function Redirect({ to }) {
  useEffect(() => {
    navigateApp(to, { replace: true });
  }, [to]);
  return null;
}

const storyPageFromUrl = (search = window.location.search) =>
  readAgentLink(search) || new URLSearchParams(search).get('view') === 'workspace'
    ? 'story'
    : 'library';
// A page's header while its code arrives; the page itself then fills in below it.
function PageWait({ account, active }) {
  return (
    <div className="home">
      <StudioHeader account={account} active={active} />
    </div>
  );
}
// The works page while the sign-in read or its code is out: real header, cards still to come.
function WorksWait() {
  const { t } = useI18n();
  return (
    <div className="home">
      <StudioHeader active="works" />
      <main className="home-main works-page">
        <header className="home-head">
          <h1 className="page-title">
            <LayoutGrid size={22} />
            {t('v2.myStories')}
          </h1>
        </header>
        <div className="story-grid">
          {[0, 1, 2, 3].map((i) => (
            <i key={i} className="ui-wait story-card" />
          ))}
        </div>
      </main>
    </div>
  );
}

function AppContent() {
  const { t } = useI18n(),
    c = useCloud(),
    account = c.account;
  useEffect(() => installPersistenceGuard(store), []);
  // Someone signed in, or back after signing in before, is about to need the workspace: start
  // fetching it now, and the main create page while idle, so neither waits for a press.
  const expectsWorkspace = Boolean(account || lastAccount());
  useEffect(() => {
    if (!expectsWorkspace) return;
    void import('./SeriesApp.jsx').catch(() => {});
    const later = globalThis.requestIdleCallback || ((run) => setTimeout(run, 800));
    later(() => {
      void import('./QuickCreateEntry.jsx').catch(() => {});
      void import('./StoryChat.jsx').catch(() => {});
    });
  }, [expectsWorkspace]);
  // Pages with their own client report a refused session; the shared check signs out once.
  useEffect(() => {
    const recheck = () => {
      if (cloud.getState().account) void cloud.recheckSession();
    };
    window.addEventListener(SESSION_REFUSED, recheck);
    return () => window.removeEventListener(SESSION_REFUSED, recheck);
  }, []);
  useEffect(() => {
    if (account || c.sessionReady) rememberAccount(account);
  }, [account, c.sessionReady]);
  const workspace = useSyncExternalStore(store.subscribe, store.getState, store.getState),
    workspaceKey = [
      workspace.workspace.mode,
      workspace.workspace.account || '',
      workspace.project.id,
      workspace.workspaceEpoch,
    ].join(':');
  const location = useAppLocation(),
    path = location.path,
    sidebarView = storyPageFromUrl(location.search);
  const target = readAgentLink(location.search),
    routeLoader = useRef(null);
  if (!routeLoader.current) routeLoader.current = createStoryRouteLoader({ cloud, store });
  const targetProjectId = target?.projectId;
  useEffect(() => {
    void routeLoader.current.update({
      account,
      projectId: targetProjectId,
      active: path === '/',
      busy: c.busy || !c.sessionReady,
    });
    // workspaceKey: re-check when the open story changes underneath the same link.
  }, [account, path, targetProjectId, c.busy, c.sessionReady, workspaceKey]);
  useEffect(() => () => routeLoader.current.destroy(), []);
  useEffect(() => installAppNavigation(), []);
  useEffect(() => {
    void cloud.initialize();
  }, []);
  useEffect(() => {
    const opened = (event) => {
      const href = openedStoryHref(
        window.location.href,
        event.detail,
        store.getState(),
        cloud.getState().account,
      );
      if (href) navigateApp(href, { restore: false });
    };
    window.addEventListener('inkseq-cloud-project-opened', opened);
    return () => window.removeEventListener('inkseq-cloud-project-opened', opened);
  }, []);
  const changePage = (next, { selected = false, projectId } = {}) => {
    const url = new URL(window.location.href);
    if (next === 'library') {
      for (const key of ['view', 'project', 'entity', 'panel']) url.searchParams.delete(key);
    } else {
      url.searchParams.set('view', 'workspace');
      if (selected) {
        const live = store.getState();
        url.searchParams.delete('entity');
        url.searchParams.delete('panel');
        if (projectId) url.searchParams.set('project', projectId);
        else if (live.workspace.mode === 'cloud') url.searchParams.set('project', live.project.id);
        else url.searchParams.delete('project');
      }
    }
    navigateApp('/' + url.search, { restore: false });
  };
  const pageFocusMounted = useRef(false);
  useEffect(() => {
    if (!pageFocusMounted.current) {
      pageFocusMounted.current = true;
      return;
    }
    if (path === '/')
      document
        .querySelector(sidebarView === 'library' ? '#story-library-heading' : '.story-home-back')
        ?.focus({ preventScroll: true });
  }, [path, sidebarView]);
  const routes = {
    '/': ({ location: page }) => {
      // Creating needs an account: signed-out visitors see the landing page.
      // The landing page is public, so it never waits for the sign-in read (#1031).
      if (!account)
        return c.sessionReady || !lastAccount() ? (
          <Landing />
        ) : readAgentLink(page.search) ? (
          <React.Suspense fallback={<PageWait active="works" />}>
            <StoryOpening
              key={'opening:' + readAgentLink(page.search).projectId}
              projectId={readAgentLink(page.search).projectId}
              account={lastAccount()}
              onBack={() => changePage('library')}
              onSidebarView={changePage}
            />
          </React.Suspense>
        ) : (
          <WorksWait />
        );
      const destination = readAgentLink(page.search),
        here =
          destination &&
          account &&
          workspace.workspace.mode === 'cloud' &&
          workspace.workspace.account === account &&
          workspace.project.id === destination.projectId;
      return destination && !here ? (
        <React.Suspense fallback={<PageWait account={account} active="works" />}>
          <StoryOpening
            key={'opening:' + destination.projectId}
            projectId={destination.projectId}
            title={c.projects.find((row) => row.id === destination.projectId)?.title}
            account={account}
            onBack={() => changePage('library')}
            onSidebarView={changePage}
          />
        </React.Suspense>
      ) : (
        <React.Suspense
          fallback={
            storyPageFromUrl(page.search) === 'story' ? (
              <PageWait account={account} active="works" />
            ) : (
              <WorksWait />
            )
          }
        >
          <SeriesApp
            key={workspaceKey}
            sidebarView={storyPageFromUrl(page.search)}
            onSidebarView={changePage}
          />
        </React.Suspense>
      );
    },
    '/app': ({ location: page }) => routes['/']({ location: page }),
    '/index.html': ({ location: page }) => routes['/']({ location: page }),
    // Any other address: the app's own page, in the person's language (#1024).
    '*': () => (
      <div className="home">
        <StudioHeader account={account} />
        <main className="home-main">
          <div className="ui-empty">
            <b role="heading" aria-level={1}>
              {t('notFound.title')}
            </b>
            <p>{t('notFound.body')}</p>
            <Button variant="primary" onClick={() => navigateApp('/')}>
              {t(account ? 'notFound.works' : 'notFound.home')}
            </Button>
          </div>
        </main>
      </div>
    ),
    '/library': ({ active }) => (
      <React.Suspense fallback={<PageWait account={account} active="library" />}>
        <Library
          key={account || 'signed-out'}
          account={account}
          active={active}
          checking={!c.sessionReady}
        />
      </React.Suspense>
    ),
    '/trash': ({ active }) => (
      <React.Suspense fallback={<PageWait account={account} active="trash" />}>
        <UniversalTrash active={active} />
      </React.Suspense>
    ),
    '/pricing': () => <PricingPage account={account} />,
    '/connect/telegram': ({ active }) => (
      <React.Suspense fallback={<InkLoader label={t('telegram.pageTitle')} />}>
        <ConnectTelegramPage account={account} active={active} />
      </React.Suspense>
    ),
    '/terms': () => <LegalPage account={account} doc="terms" />,
    '/privacy': () => <LegalPage account={account} doc="privacy" />,
    '/content': () => <LegalPage account={account} doc="content" />,
    '/settings': ({ active }) => (
      <React.Suspense fallback={<PageWait account={account} active="settings" />}>
        <SpendingSettings key={account || 'signed-out'} account={account} active={active} />
      </React.Suspense>
    ),
    '/admin': () => (
      <React.Suspense
        fallback={
          <div className="home">
            <StudioHeader account={account} active="admin" />
          </div>
        }
      >
        <AdminHome account={account} />
      </React.Suspense>
    ),
    '/operator': ({ active }) => (
      <React.Suspense fallback={<InkLoader label={t('loading.operator')} />}>
        <OperatorConsole active={active} />
      </React.Suspense>
    ),
    '/quick-chat': ({ location: page }) => (
      <React.Suspense
        fallback={
          <div className="home sc-shell">
            <StudioHeader account={account} active="quick" />
          </div>
        }
      >
        {/* ?story=preview: sample replies; otherwise the chat story builder (#422). The
            original Quick Create was removed (owner, 2026-10-24): its old links open the
            story the conversation became, its preferences moved to Settings. */}
        {(() => {
          const params = new URLSearchParams(page.search);
          const story = params.get('story');
          if (story === 'preview') return <StoryChat account={account} />;
          if (params.has('preferences')) return <Redirect to="/settings?tab=defaults" />;
          if (['session', 'card', 'revision', 'submission'].some((key) => params.has(key)))
            return <LegacyQuickChatLink account={account} sessionId={params.get('session')} />;
          return <QuickCreateEntry account={account} story={story} />;
        })()}
      </React.Suspense>
    ),
    '/freestyle': ({ active }) => (
      <React.Suspense
        fallback={
          <div className="home">
            <StudioHeader account={account} />
          </div>
        }
      >
        <Freestyle key={workspaceKey} active={active} />
      </React.Suspense>
    ),
  };
  return (
    <div className="app">
      <TrashProvider account={account}>
        <PersistenceWarning key={'persistence:' + workspaceKey} />
        <MaintenanceBanner />
        <PageErrorBoundary resetKey={location.pathname + location.search}>
          <AppRouteCache location={location} routes={routes} />
        </PageErrorBoundary>
        <LowCreditsDialog />
        <TermsGate account={account} />
        <FloatingNotices />
      </TrashProvider>
    </div>
  );
}

export default function App() {
  const account = useCloud().account;
  return (
    <I18nProvider account={account || ''}>
      <AppContent />
    </I18nProvider>
  );
}
