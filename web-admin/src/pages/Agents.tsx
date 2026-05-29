import React,{useEffect,useState,useCallback} from 'react';
import {Card,Row,Col,Input,Select,Button,Space,Tag,Spin,Modal,Form,message,Drawer,Descriptions,Switch,Empty,Typography,Tooltip} from 'antd';
import {PlusOutlined,EditOutlined,DeleteOutlined,ReloadOutlined,SearchOutlined} from '@ant-design/icons';
import {listAgents,registerAgent,unregisterAgent,toggleAgent,updateAgent} from '../api/client';
import StatusTag from '../components/StatusTag';
const {Text}=Typography;
const Agents:React.FC=()=>{
  const [ag,setAg]=useState<any[]>([]);const [tot,setTot]=useState(0);const [lo,setLo]=useState(true);
  const [sr,setSr]=useState('');const [sf,setSf]=useState('');const [vm,setVm]=useState<'grid'|'table'>('grid');
  const [ro,setRo]=useState(false);const [eo,setEo]=useState(false);const [sa,setSa]=useState<any>(null);
  const [dd,setDd]=useState(false);const [f]=Form.useForm();const [ef]=Form.useForm();
  const fetch=useCallback(async()=>{
    setLo(true);try{const d=await listAgents({limit:200,...(sr?{q:sr}:{})});
    let items=d.agents||[];if(sf==='alive')items=items.filter((a:any)=>a.connection==='websocket'||a.status==='alive');
    else if(sf==='stale')items=items.filter((a:any)=>a.status==='stale');
    else if(sf==='disabled')items=items.filter((a:any)=>a.disabled);
    setAg(items);setTot(d.total||items.length);}catch(e){console.warn('[Agents] fetch failed',e)}finally{setLo(false);}
  },[sr,sf]);
  useEffect(()=>{fetch();},[fetch]);
  const reg=async(v:any)=>{try{await registerAgent(v);message.success('Registered');setRo(false);f.resetFields();fetch();}catch(e:any){message.error(e?.response?.data?.detail||'Failed');}};
  const del=(a:any)=>{Modal.confirm({title:'Unregister Agent',content:`Unregister "${a.name||a.id}"?`,okText:'Unregister',okType:'danger',onOk:async()=>{try{await unregisterAgent(a.id);message.success('Unregistered');fetch();}catch(e:any){message.error(e?.response?.data?.detail||'Failed');}}});};
  const tog=async(a:any)=>{try{const r=await toggleAgent(a.id);message.success(r.disabled?'Disabled':'Enabled');fetch();}catch(e:any){message.error(e?.response?.data?.detail||'Failed');}};
  const edt=async(v:any)=>{if(!sa)return;try{await updateAgent(sa.id,v);message.success('Updated');setEo(false);setSa(null);ef.resetFields();fetch();}catch(e:any){message.error(e?.response?.data?.detail||'Failed');}};
  const openEdit=(a:any)=>{setSa(a);ef.setFieldsValue({name:a.name,description:a.description||'',url:a.url||''});setEo(true);};
  const sk=(a:any)=>{const s=a.capabilities?.skills||a.skills||[];if(!s.length)return null;return React.createElement(Space,{size:4,wrap:true},...s.slice(0,3).map((sk:any)=>React.createElement(Tag,{key:sk.id||sk.name,style:{borderRadius:6,fontSize:11,background:'rgba(0,122,255,0.08)',color:'#007AFF',border:'none'}},sk.name||sk.id)),s.length>3?React.createElement(Tag,{style:{borderRadius:6,fontSize:11}},`+${s.length-3}`):null);};
  const cols=[
    {title:'Name',dataIndex:'name',render:(n:any,r:any)=>React.createElement('span',{style:{fontWeight:500}},n||r.id?.substring(0,16))},
    {title:'ID',dataIndex:'id',render:(id:string)=>React.createElement('code',{style:{fontSize:11}},id?.substring(0,16)+'\u2026')},
    {title:'Status',render:(_:any,r:any)=>r.connection==='websocket'?React.createElement(StatusTag,{status:'alive',pulse:true}):React.createElement(StatusTag,{status:r.disabled?'disabled':(r.status||'offline')})},
    {title:'Actions',render:(_:any,r:any)=>React.createElement(Space,null,
      React.createElement(Button,{size:'small',icon:React.createElement(EditOutlined),onClick:()=>openEdit(r)}),
      React.createElement(Switch,{size:'small',checked:!r.disabled,onChange:()=>tog(r)}),
      React.createElement(Button,{size:'small',danger:true,icon:React.createElement(DeleteOutlined),onClick:()=>del(r)}))},
  ];
  return React.createElement('div',null,
    React.createElement('div',{style:{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:20}},
      React.createElement(Typography.Title,{level:3,style:{margin:0,fontWeight:600,fontSize:22}},
        'Agents',React.createElement('span',{style:{fontSize:13,fontWeight:400,color:'var(--text-secondary)',marginLeft:12}},`${tot} total`)),
      React.createElement(Button,{type:'primary',icon:React.createElement(PlusOutlined),onClick:()=>setRo(true)},'Register Agent')),
    React.createElement(Card,{bodyStyle:{padding:'12px 16px'},style:{marginBottom:16,borderRadius:10}},
      React.createElement(Row,{gutter:[12,12],align:'middle'},
        React.createElement(Col,{xs:24,sm:8,md:6},React.createElement(Input,{prefix:React.createElement(SearchOutlined,{style:{color:'var(--text-tertiary)'}}),placeholder:'Search agents...',value:sr,onChange:(e:any)=>setSr(e.target.value),allowClear:true})),
        React.createElement(Col,{xs:12,sm:6,md:4},React.createElement(Select,{value:sf,onChange:setSf,style:{width:'100%'},placeholder:'All Status'},React.createElement(Select.Option,{value:''},'All Status'),React.createElement(Select.Option,{value:'alive'},'Online'),React.createElement(Select.Option,{value:'stale'},'Stale'),React.createElement(Select.Option,{value:'disabled'},'Offline'))),
        React.createElement(Col,{xs:12,sm:6,md:4},React.createElement(Button,{icon:React.createElement(ReloadOutlined),onClick:fetch},'Refresh')))),
    React.createElement(Spin,{spinning:lo},ag.length===0&&!lo?React.createElement(Card,{style:{borderRadius:10,textAlign:'center',padding:40}},React.createElement(Empty,{description:'No agents found'})):
      React.createElement(Card,{bodyStyle:{padding:0},style:{borderRadius:10}},
        React.createElement('table',{className:'ant-table',style:{width:'100%'}},
          React.createElement('thead',null,React.createElement('tr',null,...cols.map(c=>React.createElement('th',{key:(c as any).title as string,style:{padding:'12px 16px',fontSize:11,textTransform:'uppercase',color:'var(--text-secondary)',textAlign:'left'}},(c as any).title)))),
          React.createElement('tbody',null,...ag.map((a:any,i:number)=>React.createElement('tr',{key:a.id,style:{cursor:'pointer',borderBottom:'1px solid var(--separator)',background:i%2===0?'transparent':'rgba(0,0,0,0.02)'},onClick:()=>{setSa(a);setDd(true);}},...cols.map(c=>React.createElement('td',{key:(c as any).title as string,style:{padding:'12px 16px',fontSize:13}},(c as any).render((c as any).dataIndex ? a[(c as any).dataIndex] : null, a)))))))),
    React.createElement(Modal,{title:'Register Agent',open:ro,onCancel:()=>{setRo(false);f.resetFields();},onOk:()=>f.submit(),okText:'Register'},
      React.createElement(Form,{form:f,layout:'vertical',onFinish:reg},
        React.createElement(Form.Item,{name:'name',label:'Name',rules:[{required:true}]},React.createElement(Input,{placeholder:'My Agent'})),
        React.createElement(Form.Item,{name:'description',label:'Description'},React.createElement(Input.TextArea,{rows:3,placeholder:'Description'})),
        React.createElement(Form.Item,{name:'url',label:'URL'},React.createElement(Input,{placeholder:'http://localhost:9001'})))),
    React.createElement(Modal,{title:`Edit: ${sa?.name||sa?.id}`,open:eo,onCancel:()=>{setEo(false);setSa(null);ef.resetFields();},onOk:()=>ef.submit(),okText:'Save'},
      React.createElement(Form,{form:ef,layout:'vertical',onFinish:edt},
        React.createElement(Form.Item,{name:'name',label:'Name'},React.createElement(Input,null)),
        React.createElement(Form.Item,{name:'description',label:'Description'},React.createElement(Input.TextArea,{rows:3})),
        React.createElement(Form.Item,{name:'url',label:'URL'},React.createElement(Input,null)))),
    React.createElement(Drawer,{title:sa?.name||'Agent Detail',placement:'right',width:480,onClose:()=>{setDd(false);setSa(null);},open:dd},
      sa&&React.createElement(Descriptions,{column:1,size:'small',bordered:true},
        React.createElement(Descriptions.Item,{label:'ID'},React.createElement('code',{style:{fontSize:11}},sa.id)),
        React.createElement(Descriptions.Item,{label:'Status'},sa.connection==='websocket'?React.createElement(StatusTag,{status:'alive',pulse:true}):React.createElement(StatusTag,{status:sa.disabled?'disabled':(sa.status||'unknown')})),
        React.createElement(Descriptions.Item,{label:'Name'},sa.name),
        React.createElement(Descriptions.Item,{label:'URL'},sa.url||'-'),
        React.createElement(Descriptions.Item,{label:'Disabled'},React.createElement(Switch,{checked:sa.disabled,disabled:true})),
        React.createElement(Descriptions.Item,{label:'Tags'},sa.tags?.join(', ')||'-')))));
};
export default Agents;
