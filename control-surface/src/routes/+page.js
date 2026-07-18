import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';

// The console opens on Overview. base is '' in dev, '/console' in the same-origin gateway build.
export const load = () => {
  throw redirect(307, `${base}/overview`);
};
