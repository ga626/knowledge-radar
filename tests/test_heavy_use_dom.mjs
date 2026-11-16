import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

test('Liepin recovers salary and bracketed location without inventing company',()=>{
 const card={innerText:'AI 业务平台开发工程师\n【\n上海-青浦区\n】\n急聘\n12-20k\n5年以上\n统招本科',querySelector:()=>null};
 const anchor={href:'https://www.liepin.com/job/1986731979.shtml',innerText:'AI 业务平台开发工程师',
  getBoundingClientRect:()=>({width:200,height:30}),closest:()=>card};
 const document={querySelector:()=>null,querySelectorAll:s=>s.startsWith('a[href*="/job/"]')?[anchor]:[],createElement:()=>({set innerHTML(x){},textContent:''})};
 const context={document,location:{href:'https://www.liepin.com/zhaopin/',pathname:'/zhaopin/',hostname:'www.liepin.com'},URL,KRSites:{}};
 vm.runInNewContext(readFileSync(new URL('../browser_link/extension/sites/liepin.js',import.meta.url),'utf8'),context);
 const result=context.KRSites.liepin.extract().items[0];
 assert.equal(result.salary,'12-20k');assert.equal(result.area,'上海-青浦区');
 assert.equal(result.experience,'5年以上');assert.equal(result.company,'');
 assert.equal(result.field_sources.salary,'visible_card_salary_line');
});

test('Maimai comments find sibling list and do not claim empty or exhaustion',async()=>{
 const rich={innerText:'有价值的评论'};
 const row={innerText:'有价值的评论',querySelector:s=>s==='[data-rich-text]'?rich:null,
  querySelectorAll:s=>s==='[data-rich-text]'?[rich]:[],getAttribute:()=> 'comment-1'};
 const body={};
 const container={innerText:'评论 有价值的评论',children:[row],parentElement:body,
  querySelector:()=>rich};
 const wrapper={innerText:'评论',children:[],querySelector:()=>null,parentElement:container};
 const heading={innerText:'评论',children:[],parentElement:wrapper,scrollIntoView:()=>{}};
 const document={body,querySelectorAll:s=>s==='div'?[heading]:[]};
 const context={document,location:{href:'https://maimai.cn/feed-detail/123',pathname:'/feed-detail/123'},
  KRSites:{},KRWaitFor:async()=>true,KRWaitForChange:async()=>true};
 context.globalThis=context;
 vm.runInNewContext(readFileSync(new URL('../browser_link/extension/sites/maimai.js',import.meta.url),'utf8'),context);
 const result=await context.KRSites.maimai.extract('comments',{},new AbortController().signal);
 assert.equal(result.comments.length,1);
 assert.equal(result.comments[0].id,'comment-1');
 assert.equal(result.comments_empty_confirmed,false);
 assert.equal(result.comments_observation.source_exhausted,false);
});
