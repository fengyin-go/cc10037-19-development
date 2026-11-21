import { createRouter, createWebHistory } from 'vue-router'

import BlockingRootCauseView from '@/views/BlockingRootCauseView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径需放在通配的 :moduleKey 之前，否则会被模块工作台抢先匹配
    { path: '/blocking-root-cause', name: 'blocking-root-cause', component: BlockingRootCauseView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
