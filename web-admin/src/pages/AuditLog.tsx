import React,{useEffect,useState,useCallback} from 'react';
import {Card,Table,Row,Col,Select,Spin,Empty,Typography,Button} from 'antd';
import {ReloadOutlined} from '@ant-design/icons';
import {listAuditLogs} from '../api/client';
import StatusTag from '../components/StatusTag';
const{Title,Text}=Typography;
const AuditLog:React.FC=()=>{
  const[ls,setLs]=useState<any[]>([]);const[lo,setLo]=useState(true);const[ty,setTy]=useState('');const[ae,setAe]=useState('');
  const fetch=useCallback(async()=>{
    setLo(true);try{const d=await listAuditLogs({limit:200,type:ty||undefined,actor:ae||undefined});setLs(d.events||d.logs||[]);}catch(e){console.warn('[AuditLog] fetch failed',e)}finally{setLo(false);}
  },[ty,ae]);
  useEffect(()=>{fetch();},[fetch]);
  const EV:{[k:string]:string}={agent_register:'blue',agent_unregister:'red',agent_toggle:'orange',task_create:'blue',task_complete:'green',client_create:'blue',client_delete:'red'};
  return React.createElement('div',null,
    React.createElement('div',{style:{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:20}},
      React.createElement(Title,{level:3,style:{margin:0,fontWeight:600,fontSize:22}},'Audit Log',React.createElement('span',{style:{fontSize:13,fontWeight:400,color:'var(--text-secondary)',marginLeft:12}},`${ls.length} entries`)),
      React.createElement(Button,{icon:React.createElement(ReloadOutlined),onClick:fetch},'Refresh')),
    React.createElement(Card,{bodyStyle:{padding:'12px 16px'},style:{marginBottom:16,borderRadius:10}},
      React.createElement(Row,{gutter:[12,12],align:'middle'},
        React.createElement(Col,null,React.createElement('span',{style:{fontSize:12,color:'var(--text-secondary)',marginRight:8}},'Type:'),React.createElement(Select,{value:ty,onChange:setTy,style:{width:180},allowClear:true},React.createElement(Select.Option,{value:''},'All'),React.createElement(Select.Option,{value:'agent_register'},'Agent Register'),React.createElement(Select.Option,{value:'agent_unregister'},'Agent Unregister'),React.createElement(Select.Option,{value:'agent_toggle'},'Agent Toggle'),React.createElement(Select.Option,{value:'task_create'},'Task Create'),React.createElement(Select.Option,{value:'task_complete'},'Task Complete'),React.createElement(Select.Option,{value:'client_create'},'Client Create'),React.createElement(Select.Option,{value:'client_delete'},'Client Delete'))),
        React.createElement(Col,null,React.createElement('span',{style:{fontSize:12,color:'var(--text-secondary)',marginRight:8}},'Actor:'),React.createElement(Select,{value:ae,onChange:setAe,style:{width:150},allowClear:true},React.createElement(Select.Option,{value:''},'All'),React.createElement(Select.Option,{value:'system'},'System'),React.createElement(Select.Option,{value:'dashboard'},'Dashboard'))))),
    React.createElement(Card,{bodyStyle:{padding:0},style:{borderRadius:10}},
      React.createElement(Spin,{spinning:lo},ls.length===0&&!lo?React.createElement(Empty,{description:'No audit entries',style:{padding:60}}):
        React.createElement(Table,{dataSource:ls,columns:[{title:'Time',dataIndex:'created_at',width:180,render:(t:number)=>new Date((t||Date.now()/1000)*1000).toLocaleString()},{title:'Type',dataIndex:'event_type',width:140,render:(e:string)=>React.createElement(StatusTag,{status:e,color:EV[e]||'default'})},{title:'Actor',dataIndex:'actor_id',width:150,render:(a:string)=>React.createElement(Text,{code:true},{style:{fontSize:11}},a?.substring(0,16))},{title:'Target',dataIndex:'target_id',width:150,render:(t:string)=>React.createElement(Text,{code:true},{style:{fontSize:10}},t)},{title:'Detail',dataIndex:'detail',render:(d:string)=>React.createElement('div',{style:{maxWidth:400,fontSize:11,color:'var(--text-secondary)'}},d||'-')}],rowKey:'id',pagination:{pageSize:30,showTotal:(t:number)=>`${t} entries`},size:'small',scroll:{x:800}}))));
};
export default AuditLog;
