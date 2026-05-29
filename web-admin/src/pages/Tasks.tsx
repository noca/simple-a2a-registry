import React,{useEffect,useState,useCallback} from 'react';
import {Card,Table,Button,Row,Col,Select,Spin,Empty,Typography,Drawer,Descriptions} from 'antd';
import {ReloadOutlined} from '@ant-design/icons';
import {listV1Tasks} from '../api/client';
import StatusTag from '../components/StatusTag';
const{Title}=Typography;
const Tasks:React.FC=()=>{
  const[ts,setTs]=useState<any[]>([]);const[lo,setLo]=useState(true);const[sf,setSf]=useState('');const[dt,setDt]=useState<any>(null);const[dd,setDd]=useState(false);
  const fetch=useCallback(async()=>{
    setLo(true);try{const d=await listV1Tasks(sf?{state:sf}:{});setTs(d.tasks||[]);}catch(e){console.warn('[Tasks] fetch failed',e);setTs([])}finally{setLo(false);}
  },[sf]);
  useEffect(()=>{fetch();},[fetch]);
  return React.createElement('div',null,
    React.createElement('div',{style:{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:20}},
      React.createElement(Title,{level:3,style:{margin:0,fontWeight:600,fontSize:22}},'Tasks',React.createElement('span',{style:{fontSize:13,fontWeight:400,color:'var(--text-secondary)',marginLeft:12}},`${ts.length} total`)),
      React.createElement(Button,{icon:React.createElement(ReloadOutlined),onClick:fetch},'Refresh')),
    React.createElement(Card,{bodyStyle:{padding:'12px 16px'},style:{marginBottom:16,borderRadius:10}},
      React.createElement(Row,{gutter:12,align:'middle'},
        React.createElement(Col,null,React.createElement('span',{style:{fontSize:12,color:'var(--text-secondary)',marginRight:8}},'State:'),React.createElement(Select,{value:sf,onChange:setSf,style:{width:150}},React.createElement(Select.Option,{value:'',children:'All'}),React.createElement(Select.Option,{value:'dispatched',children:'Dispatched'}),React.createElement(Select.Option,{value:'forwarded',children:'Forwarded'}),React.createElement(Select.Option,{value:'working',children:'Working'}),React.createElement(Select.Option,{value:'completed',children:'Completed'}),React.createElement(Select.Option,{value:'failed',children:'Failed'}))))),
    React.createElement(Spin,{spinning:lo},ts.length===0&&!lo?React.createElement(Card,{style:{borderRadius:10,textAlign:'center',padding:40}},React.createElement(Empty,{description:'No tasks'})):
      React.createElement(Card,{bodyStyle:{padding:0},style:{borderRadius:10}},
        React.createElement(Table,{dataSource:ts,columns:[{title:'ID',dataIndex:'id',render:(id:string)=>React.createElement('code',{style:{fontSize:11}},id?.substring(0,12)+'...')},{title:'Agent',dataIndex:'agent_id',render:(a:string)=>React.createElement('span',{style:{fontWeight:500}},a)},{title:'Query',dataIndex:'query',render:(q:string)=>React.createElement('div',{style:{maxWidth:300,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',fontSize:12}},q?q?.substring(0,60)+(q?.length>60?'...':''):'-')},{title:'State',dataIndex:'state',render:(s:string)=>React.createElement(StatusTag,{status:s,pulse:s==='working'||s==='forwarded'})},{title:'Created',dataIndex:'created_at',render:(t:number)=>t?new Date(t*1000).toLocaleString():'-'}],rowKey:'id',pagination:{pageSize:20,showTotal:(t:number)=>`${t} tasks`},size:'middle',onRow:(r:any)=>({onClick:()=>{setDt(r);setDd(true);},style:{cursor:'pointer'}})}))),
    React.createElement(Drawer,{title:`Task: ${dt?.id?.substring(0,12)}...`,placement:'right',width:480,onClose:()=>{setDd(false);setDt(null);},open:dd},
      dt&&React.createElement(Descriptions,{column:1,size:'small',bordered:true},
        React.createElement(Descriptions.Item,{label:'ID'},React.createElement('code',{style:{fontSize:11}},dt.id)),
        React.createElement(Descriptions.Item,{label:'Agent'},dt.agent_id),
        React.createElement(Descriptions.Item,{label:'State'},React.createElement(StatusTag,{status:dt.state,pulse:dt.state==='working'||dt.state==='forwarded'})),
        React.createElement(Descriptions.Item,{label:'Created'},dt.created_at?new Date(dt.created_at*1000).toLocaleString():'-'),
        React.createElement(Descriptions.Item,{label:'Updated'},dt.updated_at?new Date(dt.updated_at*1000).toLocaleString():'-'),
        React.createElement(Descriptions.Item,{label:'Error'},dt.error||'-'),
        React.createElement(Descriptions.Item,{label:'Result'},dt.result||'-'))));
};
export default Tasks;
