// Every style the first script carries, in the order it has always had. Pages that load later
// keep their own imports; listing the styles here stops moving a page from reordering the CSS.
import './ink-loader.css';
import './ui/app-nav.css';
import './ui/menu.css';
import './v2/ops-adapter.css';
import './i18n/workspace-language.css';
import './cloud-studio.css';
import './api-keys.css';
import './credits.css';
import './account-menu.css';
import './admin.css';
import './report-problem.css';
import './public-pages.css';
import './landing.css';
import './landing-parts.css';
import './ui/side-nav.css';
import './v2/library-adapter.css';
import './ui/settings.css';
import './project-activity.css';
import './v2/story-film.css';
import './v2/story-view.css';
import './guide.css';
import './inky/inky.css';
import './ui/filter-chip.css';
import './profile-editor.css';
import './trim-bar.css';
import './shot-segments.css';
import './take-review.css';
import './v2/works-adapter.css';
import './story-open.css';
import './persistence-warning.css';
import './operator-budget.css';
import './capacity-settings.css';
import './maintenance.css';
import './floating-notices.css';
import './ui/tokens.css';
import './ui/controls.css';
import './ui/density.css';
import './ui/feedback.css';
import './demo-v2.css';
import './v2/page-bridge.css';
import './ui/dialog.css';
import './skins.css';
// Shared ui/ primitives ship with the first script too: a primitive's stylesheet that came in a
// later chunk arrived after the storyboard was drawn, and every cast chip grew a 1px border
// (cards grew 10px at random, h3-studio layout audit). Last here, where the late chunk used
// to put them, so the cascade is unchanged.
import './ui/chip.css';
import './ui/list-button.css';
import './ui/tabs.css';
