import React,{useEffect,useState,useCallback} from 'react';
import {Card,Table,Button,Row,Col,Input,Spin,Empty,Typography,Space,Tag,Switch,Modal,message,Form} from 'antd';
import {PlusOutlined,ReloadOutlined,DeleteOutlined,SearchOutlined} from '@ant-design/icons';
import {listClients,createClient,deleteClient,toggleClient} from '../api/client';
import StatusTag from '../components/StatusTag';
const{Text}=Typography;
const Clients:React.FC=()=>{
  const[cl,setCl]=useState<any[]>([]);const[lo,setLo]=useState(true);const[sr,setSr]=useState('');
  const[co,setCo]=useState(false);const[f]=Form.useForm();
  const fetch=useCallback(async()=>{
    setLo(true);try{const d=await listClients({limit:200,...(sr?{q:sr}:{})});setCl(Array.isArray(d)?d:(d.clients||[]));}catch(e){console.warn('[Clients] fetch failed',e)}finally{setLo(false);}
  },[sr]);
  useEffect(()=>{fetch();},[fetch]);
  const create=async(v:any)=>{try{await createClient(v);message.success('Created');setCo(false);f.resetFields();fetch();}catch(e:any){message.error(e?.response?.data?.detail||'Failed');}};
  const del=(id:string)=>Modal.confirm({title:'Delete Client',content:'This cannot be undone.',okText:'Delete',okType:'danger',onOk:async()=>{try{await deleteClient(id);message.success('Deleted');fetch();}catch(e:any){message.error(e?.response?.data?.detail||'Failed');}}});
  const tog=async(id:string)=>{try{const r=await toggleClient(id);message.success(r.disabled?'Disabled':'Enabled');fetch();}catch(e:any){message.error(e?.response?.data?.detail||'Failed');}};
  return React.createElement('div',null,
    React.createElement('div',{style:{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:20}},
      React.createElement(Typography.Title,{level:3,style:{margin:0,fontWeight:600,fontSize:22}},'Clients',React.createElement('span',{style:{fontSize:13,fontWeight:400,color:'var(--text-secondary)',marginLeft:12}},`${cl.length} total`)),
      React.createElement(Button,{type:'primary',icon:React.createElement(PlusOutlined),onClick:()=>setCo(true)},'Create Client')),
    React.createElement(Card,{bodyStyle:{padding:'12px 16px'},style:{marginBottom:16,borderRadius:10}},
      React.createElement(Row,{gutter:[12,12],align:'middle'},
        React.createElement(Col,{xs:18,sm:12,md:8},React.createElement(Input,{prefix:React.createElement(SearchOutlined,{style:{color:'var(--text-tertiary)'}}),placeholder:'Search...',value:sr,onChange:(e:any)=>setSr(e.target.value),allowClear:true})),
        React.createElement(Col,null,React.createElement(Button,{icon:React.createElement(ReloadOutlined),onClick:fetch},'Refresh')))),
    React.createElement(Spin,{spinning:lo},cl.length===0&&!lo?React.createElement(Card,{style:{borderRadius:10,textAlign:'center',padding:40}},React.createElement(Empty,{description:'No clients'})):
      React.createElement(Card,{bodyStyle:{padding:0},style:{borderRadius:10}},
        React.createElement(Table,{dataSource:cl,columns:[{title:'Name',dataIndex:'name',render:(n:any,r:any)=>React.createElement(Text,{style:{fontWeight:500}},n||r.id?.substring(0,16))},{title:'Client ID',dataIndex:'client_id',render:(c:string)=>React.createElement('code',{style:{fontSize:11}},c?.substring(0,20)+'...')},{title:'Scopes',dataIndex:'scopes',render:(s:string[])=>s?.length?s.map(sc=>React.createElement(Tag,{key:sc,style:{borderRadius:4,marginBottom:2}},sc)):'-'},{title:'Status',render:(_:any,r:any)=>React.createElement(Switch,{size:'small',checked:!r.disabled,onChange:()=>tog(r.id)})},{title:'Actions',render:(_:any,r:any)=>React.createElement(Space,null,React.createElement(Button,{size:'small',danger:true,icon:React.createElement(DeleteOutlined),onClick:()=>del(r.id)}))}],rowKey:'id',pagination:{pageSize:20,showTotal:(t:number)=>`${t} clients`},size:'middle',scroll:{x:700}}))),
    React.createElement(Modal,{title:'Create Client',open:co,onCancel:()=>{setCo(false);f.resetFields();},onOk:()=>f.submit(),okText:'Create'},
      React.createElement(Form,{form:f,layout:'vertical',onFinish:create},
        React.createElement(Form.Item,{name:'name',label:'Name',rules:[{required:true}]},React.createElement(Input,{placeholder:'My Client'})),
        React.createElement(Form.Item,{name:'description',label:'Description'},React.createElement(Input.TextArea,{rows:3})),
        React.createElement(Form.Item,{name:'scopes',label:'Scopes (comma separated)'},React.createElement(Input,{placeholder:'agents:read,metrics:write'})))));
};
export default Clients;
