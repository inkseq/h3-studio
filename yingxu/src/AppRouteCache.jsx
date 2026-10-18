import React, { createContext, useContext, useRef, useSyncExternalStore } from 'react';
import { appLocation, getAppLocation, subscribeAppLocation } from './app-navigation.js';

const PageActivity = createContext(true);
export const usePageActive = () => useContext(PageActivity);
export function useAppLocation() {
  const href = useSyncExternalStore(subscribeAppLocation, getAppLocation, () => '/');
  return appLocation(href, window.location.href);
}

// A page mounts on its first visit. Hiding it retains its controllers, loaded
// data and unsaved editor state; activity-aware effects pause while hidden.
export default function AppRouteCache({ location, routes }) {
  const visited = useRef(new Map());
  visited.current.set(location.path, location);
  return [...visited.current].map(([path, lastLocation]) => {
    const active = path === location.path,
      render = routes[path] || routes['/'];
    return (
      <PageActivity.Provider key={path} value={active}>
        <div
          data-app-route={path}
          hidden={!active}
          inert={!active}
          style={active ? undefined : { display: 'none' }}
        >
          {render({ active, location: lastLocation })}
        </div>
      </PageActivity.Provider>
    );
  });
}
