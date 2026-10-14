import * as React from 'react';
import { Outlet, createRootRoute } from '@tanstack/react-router';
import { TanStackRouterDevtools } from '@tanstack/react-router-devtools';
import { NavigationSidebar } from '../components/NavigationSidebar';
import { DeviceProvider } from '../lib/device-context';

export const Route = createRootRoute({
  component: RootComponent,
});

export function RootComponent() {
  return (
    <DeviceProvider>
      <div className="h-screen flex flex-col overflow-hidden">
        <div className="flex-1 flex overflow-hidden">
          <NavigationSidebar />
          <div className="flex-1 flex flex-col overflow-hidden">
            <div className="flex-1 overflow-auto">
              <Outlet />
            </div>
          </div>
        </div>
        {__DEVTOOLS_ENABLED__ && (
          <TanStackRouterDevtools position="bottom-right" />
        )}
      </div>
    </DeviceProvider>
  );
}
